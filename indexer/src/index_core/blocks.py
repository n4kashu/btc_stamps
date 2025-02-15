"""
initialize database.

Sieve blockchain for Stamp transactions, and add them to the database.
"""

import concurrent.futures
import decimal
import http
import logging
import sys
import time
from collections import namedtuple
from typing import List

import bitcoin as bitcoinlib
from bitcoin.core.script import CScriptInvalidError
from bitcoin.wallet import CBitcoinAddress
from bitcoinlib.keys import pubkeyhash_to_addr
from pymysql.connections import Connection

# import cProfile
# import pstats
import config
import index_core.arc4 as arc4
import index_core.backend as backend
import index_core.check as check
import index_core.log as log
import index_core.script as script
import index_core.util as util
from index_core.database import (
    initialize,
    insert_block,
    insert_into_src20_tables,
    insert_into_stamp_table,
    insert_transactions,
    is_prev_block_parsed,
    next_tx_index,
    purge_block_db,
    rebuild_balances,
    update_block_hashes,
    update_parsed_block,
)
from index_core.exceptions import BlockAlreadyExistsError, BlockUpdateError, BTCOnlyError, DatabaseInsertError, DecodeError
from index_core.models import StampData, ValidStamp
from index_core.src20 import Src20Dict  # FIXME: move to models for consistency
from index_core.src20 import (
    clear_zero_balances,
    parse_src20,
    process_balance_updates,
    update_src20_balances,
    validate_src20_ledger_hash,
)
from index_core.stamp import parse_stamp
from index_core.xcprequest import fetch_cp_concurrent, filter_issuances_by_tx_hash

D = decimal.Decimal
logger = logging.getLogger(__name__)
log.set_logger(logger)
skip_logger = logging.getLogger("list_tx.skip")

TxResult = namedtuple(
    "TxResult",
    [
        "tx_index",
        "source",
        "destination",
        "btc_amount",
        "fee",
        "data",
        "decoded_tx",
        "keyburn",
        "is_op_return",
        "tx_hash",
        "block_index",
        "block_hash",
        "block_time",
        "p2wsh_data",
    ],
)


class BlockProcessor:
    def __init__(self, db):
        self.db: Connection = db
        self.valid_stamps_in_block: List[ValidStamp] = []
        self.parsed_stamps: List[StampData] = []
        self.processed_src20_in_block: List[Src20Dict] = []

    def process_transaction_results(self, tx_results):
        for result in tx_results:
            stamp_data = StampData(
                tx_hash=result.tx_hash,
                source=result.source,
                destination=result.destination,
                btc_amount=result.btc_amount,
                fee=result.fee,
                data=result.data,
                decoded_tx=result.decoded_tx,
                keyburn=result.keyburn,
                tx_index=result.tx_index,
                block_index=result.block_index,
                block_time=result.block_time,
                is_op_return=result.is_op_return,
                p2wsh_data=result.p2wsh_data,
            )
            _, stamp_data, valid_stamp, prevalidated_src20 = parse_stamp(
                stamp_data=stamp_data,
                db=self.db,
                valid_stamps_in_block=self.valid_stamps_in_block,
            )

            if stamp_data:
                self.parsed_stamps.append(stamp_data)  # includes cursed and prevalidated src20 on CP
            if valid_stamp:
                self.valid_stamps_in_block.append(valid_stamp)
            if prevalidated_src20:
                _, src20_dict = parse_src20(self.db, prevalidated_src20, self.processed_src20_in_block)
                self.processed_src20_in_block.append(src20_dict)

        if self.parsed_stamps:
            insert_into_stamp_table(self.db, self.parsed_stamps)
            for stamp in self.parsed_stamps:
                stamp.match_and_insert_collection_data(config.LEGACY_COLLECTIONS, self.db)

    def finalize_block(self, block_index, block_time, txhash_list):
        if self.processed_src20_in_block:
            balance_updates = update_src20_balances(self.db, block_index, block_time, self.processed_src20_in_block)
            insert_into_src20_tables(self.db, self.processed_src20_in_block)
            valid_src20_str = process_balance_updates(balance_updates)
        else:
            valid_src20_str = ""

        if block_index > config.BTC_SRC20_GENESIS_BLOCK and block_index % 100 == 0:
            clear_zero_balances(self.db)

        new_ledger_hash, new_txlist_hash, new_messages_hash = create_check_hashes(
            self.db, block_index, self.valid_stamps_in_block, valid_src20_str, txhash_list
        )

        if valid_src20_str:
            validate_src20_ledger_hash(block_index, new_ledger_hash, valid_src20_str)

        stamps_in_block = len(self.valid_stamps_in_block)
        src20_in_block = len(self.processed_src20_in_block)
        return new_ledger_hash, new_txlist_hash, new_messages_hash, stamps_in_block, src20_in_block

    def insert_transactions(self, tx_results):
        insert_transactions(self.db, tx_results)


def process_vout(ctx, stamp_issuance=None):
    """
    Process all the out values of a transaction.

    Args:
        ctx (TransactionContext): The transaction context.

    Returns:
        tuple: A tuple containing the following values:
            - pubkeys_compiled (list): A list of public keys.
            - keyburn (int or None): The keyburn value of the transaction.
            - is_op_return (bool or None): Indicates if the transaction is an OP_RETURN transaction.
            - fee (int): The updated fee after processing the vout values.
    """
    pubkeys_compiled = []
    keyburn = None
    is_op_return, is_olga = None, None

    # Ignore coinbase transactions.
    if ctx.is_coinbase():
        raise DecodeError("coinbase transaction")

    # Fee is the input values minus output values.
    fee = 0

    for vout in ctx.vout:
        # asm is the bytestring of the vout values
        # Fee is the input values minus output values.
        fee -= vout.nValue
        # Ignore transactions with invalid script.
        try:
            asm = script.get_asm(vout.scriptPubKey)
        except CScriptInvalidError as e:
            raise DecodeError(e)
        if asm[-1] == "OP_CHECKMULTISIG":  # the last element in the asm list is OP_CHECKMULTISIG
            try:
                # NOTE: need to investigate signatures_required, signatures_possible
                pubkeys, signatures_required, keyburn_vout = script.get_checkmultisig(
                    asm
                )  # this is all the pubkeys from the loop
                if (
                    keyburn_vout is not None
                ):  # if one of the vouts have keyburn we set keyburn for the whole trx. the last vout is not keyburn
                    keyburn = keyburn_vout
                pubkeys_compiled += pubkeys
                # stripped_pubkeys = [pubkey[1:-1] for pubkey in pubkeys]
            except Exception as e:
                raise DecodeError(f"unrecognised output type {e}")
        elif asm[-1] == "OP_CHECKSIG":
            pass  # FIXME: not certain if we need to check keyburn on OP_CHECKSIG
            # see 'A14845889080100805000'
            #   0: OP_DUP
            #   1: OP_HASH160
            #   3: OP_EQUALVERIFY
            #   4: OP_CHECKSIG
        elif asm[0] == "OP_RETURN":
            is_op_return = True
        elif stamp_issuance and asm[0] == 0 and len(asm[1]) == 32:
            # Pay-to-Witness-Script-Hash (P2WSH)
            pubkeys = script.get_p2wsh(asm)
            pubkeys_compiled += pubkeys
            is_olga = True

    vOutInfo = namedtuple("vOutInfo", ["pubkeys_compiled", "keyburn", "is_op_return", "fee", "is_olga"])

    return vOutInfo(pubkeys_compiled, keyburn, is_op_return, fee, is_olga)


def get_tx_info(tx_hex, block_index=None, db=None, stamp_issuance=None):
    """
    Get transaction information.

    Args:
        tx_hex (str): The hexadecimal representation of the transaction.
        block_index (int, optional): The index of the block. Defaults to None.
        db (object, optional): The database object. Defaults to None.
        stamp_issuance (bool, optional): Flag indicating if the transaction is a stamp issuance. Defaults to None.

    Returns:
        TransactionInfo: A named tuple containing the transaction information.

    Raises:
        DecodeError: If the output type is unrecognized.
        BTCOnlyError: If the transaction is not a stamp.

    Note:
        The destinations, if they exist, always come before the data output, and the change, if it exists, always comes after.
        Include keyburn check on all transactions, not just src-20.
        This function parses every transaction, not just stamps/src-20.
        Returns normalized None data for DecodeError and BTCOnlyError.
    """
    TransactionInfo = namedtuple(
        "TransactionInfo",
        [
            "source",
            "destinations",
            "btc_amount",
            "fee",
            "data",
            "ctx",
            "keyburn",
            "is_op_return",
            "p2wsh_data",
        ],
    )

    try:
        if not block_index:
            block_index = util.CURRENT_BLOCK_INDEX

        destinations, btc_amount, data, p2wsh_data = [], 0, b"", b""

        ctx = backend.deserialize(tx_hex)
        vout_info = process_vout(ctx, stamp_issuance=stamp_issuance)
        pubkeys_compiled = vout_info.pubkeys_compiled
        keyburn = getattr(vout_info, "keyburn", None)
        is_op_return = getattr(vout_info, "is_op_return", None)
        fee = getattr(vout_info, "fee", None)

        if stamp_issuance is not None:
            if pubkeys_compiled and vout_info.is_olga:
                chunk = b""
                for pubkey in pubkeys_compiled:
                    chunk += pubkey
                pubkey_len = int.from_bytes(chunk[0:2], byteorder="big")
                p2wsh_data = chunk[2 : 2 + pubkey_len]
            else:
                p2wsh_data = None
            return TransactionInfo(
                None,
                None,
                btc_amount,
                round(fee),
                None,
                None,
                keyburn,
                is_op_return,
                p2wsh_data,
            )

        if pubkeys_compiled:  # this is the combination of the two pubkeys which hold the SRC-20 data
            chunk = b""
            for pubkey in pubkeys_compiled:
                chunk += pubkey[1:-1]  # Skip sign byte and nonce byte. ( this does the concatenation as well)
        try:
            src20_destination, src20_data = decode_checkmultisig(ctx, chunk)  # this only decodes src-20 type trx
        except Exception as e:
            raise DecodeError(f"unrecognized output type {e}")
        if src20_destination is None or src20_data is None:
            raise ValueError("src20_destination and src20_data must not be None")
        if src20_data is not None:
            data += src20_data
            destinations = str(src20_destination)

        if not data:
            raise BTCOnlyError("no data, not a stamp", ctx)

        vin = ctx.vin[0]

        prev_tx_hash = vin.prevout.hash
        prev_tx_index = vin.prevout.n

        # Get the full transaction data for the previous transaction.
        prev_tx = backend.getrawtransaction(util.ib2h(prev_tx_hash))
        prev_ctx = backend.deserialize(prev_tx)

        # Get the output being spent by the input.
        prev_vout = prev_ctx.vout[prev_tx_index]
        prev_vout_script_pubkey = prev_vout.scriptPubKey

        # Decode the address associated with the output.
        source = decode_address(prev_vout_script_pubkey)

        return TransactionInfo(
            str(source),
            destinations,
            btc_amount,
            round(fee),
            data,
            ctx,
            keyburn,
            is_op_return,
            None,
        )

    except (DecodeError, BTCOnlyError):
        return TransactionInfo(b"", None, None, None, None, None, None, None, None)


def decode_address(script_pubkey):
    """
    Decode a Bitcoin address from a scriptPubKey. This supports taproot, etc

    Args:
        script_pubkey (bytes): The scriptPubKey to decode.

    Returns:
        str: The decoded Bitcoin address.

    Raises:
        ValueError: If the scriptPubKey format is unsupported.
    """
    try:
        # Attempt standard address decoding
        address = CBitcoinAddress.from_scriptPubKey(script_pubkey)
        return str(address)
    except Exception:
        # Handle other types of addresses
        if len(script_pubkey) == 34 and script_pubkey[0] == 0x51:  # Taproot check
            # Extract the witness program for Taproot
            witness_program = script_pubkey[2:]
            # Decode as Bech32m address
            return pubkeyhash_to_addr(witness_program, prefix="bc", encoding="bech32", witver=1)
        else:
            raise ValueError("Unsupported scriptPubKey format")


def decode_checkmultisig(ctx, chunk):
    """
    Decode a checkmultisig transaction chunk. Decoding in ARC4 and looking for the STAMP prefix
    This also validates the length of the string with the 2 byte data length prefix

    Args:
        ctx (Context): The context object containing transaction information.
        chunk (bytes): The chunk to be decoded.

    Returns:
        tuple: A tuple containing the destination address (str) and the decoded data (bytes).
               If the chunk does not match the expected format, returns (None, None).

    Raises:
        DecodeError: If the decoded data length does not match the expected length.
    """
    key = arc4.init_arc4(ctx.vin[0].prevout.hash[::-1])
    chunk = arc4.arc4_decrypt_chunk(chunk, key)  # this is a different method since we are stripping the nonce/sign beforehand
    if chunk[2 : 2 + len(config.PREFIX)] == config.PREFIX:
        chunk_length = chunk[:2].hex()  # the expected length of the string from the first 2 bytes
        data = chunk[len(config.PREFIX) + 2 :].rstrip(b"\x00")
        data_length = len(chunk[2:].rstrip(b"\x00"))
        if data_length != int(chunk_length, 16):
            raise DecodeError("invalid data length")

        script_pubkey = ctx.vout[0].scriptPubKey
        destination = decode_address(script_pubkey)

        return str(destination), data
    else:
        return None, data


def reinitialize(db, block_index=None):
    """Not yet implemented for stamps"""

    """Drop all predefined tables and initialize the database once again."""
    cursor = db.cursor()

    # Delete all of the results of parsing (including the undolog) - removed since we aren't using any of these tables,
    # perhaps we purge from the src table here..

    # Create missing tables
    initialize(db)

    # warning and exit the program
    Exception("reinitialize() is not implemented yet")

    # clean consensus hashes if first block hash doesn't match with checkpoint.
    if config.TESTNET:
        checkpoints = check.CHECKPOINTS_TESTNET
    elif config.REGTEST:
        checkpoints = check.CHECKPOINTS_REGTEST
    else:
        checkpoints = check.CHECKPOINTS_MAINNET

    columns = [column["name"] for column in cursor.execute("""PRAGMA table_info(blocks)""")]
    for field in ["ledger_hash", "txlist_hash"]:
        if field in columns:
            sql = """SELECT {} FROM blocks  WHERE block_index = ?""".format(field)  # nosec
            first_block = list(cursor.execute(sql, (config.BLOCK_FIRST,)))
            if first_block:
                first_hash = first_block[0][field]
                if first_hash != checkpoints[config.BLOCK_FIRST][field]:
                    logger.info("First hash changed. Cleaning {}.".format(field))
                    cursor.execute("""UPDATE blocks SET {} = NULL""".format(field))  # nosec

    # For rollbacks, just delete new blocks and then reparse what’s left.
    if block_index:
        cursor.execute("""DELETE FROM transactions WHERE block_index > ?""", (block_index,))
        cursor.execute("""DELETE FROM blocks WHERE block_index > ?""", (block_index,))
    elif config.TESTNET or config.REGTEST:  # block_index NOT specified and we are running testnet
        # just blow away the consensus hashes with a full testnet reparse, as we could activate
        # new features retroactively, which could otherwise lead to ConsensusError exceptions being raised.
        logger.info("Testnet/regtest full reparse detected: Clearing all consensus hashes before performing reparse.")
        cursor.execute("""UPDATE blocks SET ledger_hash = NULL, txlist_hash = NULL, messages_hash = NULL""")

    cursor.close()


def reparse(db, block_index=None, quiet=False):
    """Reparse all transactions (atomically). If block_index is set, rollback
    to the end of that block.
    """
    Exception("reparse() is not implemented yet")

    check.cp_version()
    reparse_start = time.time()

    reparsed = False

    cursor = db.cursor()

    if not reparsed:
        if block_index:
            logger.info("Could not roll back from undolog. Performing full reparse instead...")

        if quiet:
            root_logger = logging.getLogger()
            root_level = logger.getEffectiveLevel()

        with db:
            reinitialize(db, block_index)

            # Reparse all blocks, transactions.
            if quiet:
                root_logger.setLevel(logging.WARNING)

            # previous_ledger_hash, previous_txlist_hash, previous_messages_hash = None, None, None
            cursor.execute("""SELECT * FROM blocks ORDER BY block_index""")
            for block in cursor.fetchall():
                util.CURRENT_BLOCK_INDEX = block["block_index"]
                # will need to fetch txhash_list here to parse for consensus hashes in parse_block
                # previous_ledger_hash, previous_txlist_hash, previous_messages_hash, previous_found_messages_hash = parse_block(
                #                                                          db, block['block_index'], txhash_list=None,
                #                                                          previous_ledger_hash=previous_ledger_hash,
                #                                                          previous_txlist_hash=previous_txlist_hash,
                #                                                          previous_messages_hash=previous_messages_hash)
                # if quiet and block['block_index'] % 10 == 0:  # every 10 blocks print status
                #     root_logger.setLevel(logging.INFO)
                # logger.info('Block (re-parse): %s (hashes: L:%s / TX:%s / M:%s%s)' % (
                #     block['block_index'], previous_ledger_hash[-5:], previous_txlist_hash[-5:], previous_messages_hash[-5:],
                #     (' [overwrote %s]' % previous_found_messages_hash) if previous_found_messages_hash and previous_found_messages_hash != previous_messages_hash else ''))
                if quiet and block["block_index"] % 10 == 0:
                    root_logger.setLevel(logging.WARNING)

        if quiet:
            root_logger.setLevel(root_level)

    cursor.close()
    reparse_end = time.time()
    logger.info("Reparse took {:.3f} minutes.".format((reparse_end - reparse_start) / 60.0))


def list_tx(db, block_index: int, tx_hash: str, tx_hex=None, stamp_issuance=None):
    if not isinstance(tx_hash, str):
        raise TypeError("tx_hash must be a string")
    # NOTE: this is for future reparsing options
    # cursor = db.cursor()
    # cursor.execute('''SELECT * FROM transactions WHERE tx_hash = %s''', (tx_hash,)) # this will include all CP transactinos as well ofc
    # transactions = cursor.fetchall()
    # cursor.close()
    # if transactions:
    #     return tx_index

    if tx_hex is None:
        tx_hex = backend.getrawtransaction(tx_hash)  # TODO: This is the call that is stalling the process the most

    transaction_info = get_tx_info(tx_hex, block_index=block_index, db=db, stamp_issuance=stamp_issuance)
    source = getattr(transaction_info, "source", None)
    destination = getattr(transaction_info, "destinations", None)
    btc_amount = getattr(transaction_info, "btc_amount", None)
    fee = getattr(transaction_info, "fee", None)
    data = getattr(transaction_info, "data", None)
    decoded_tx = getattr(transaction_info, "ctx", None)
    keyburn = getattr(transaction_info, "keyburn", None)
    is_op_return = getattr(transaction_info, "is_op_return", None)
    p2wsh_data = getattr(transaction_info, "p2wsh_data", None)

    if block_index != util.CURRENT_BLOCK_INDEX:
        raise ValueError(f"block_index does not match util.CURRENT_BLOCK_INDEX: {block_index} != {util.CURRENT_BLOCK_INDEX}")

    if stamp_issuance is not None:
        source = str(stamp_issuance["source"])
        destination = str(stamp_issuance["issuer"])
        data = str(stamp_issuance)

    if source and (data or destination):
        logger.info(
            "Saving to MySQL transactions: {}\nDATA:{}\nKEYBURN: {}\nOP_RETURN: {}".format(
                tx_hash, data, keyburn, is_op_return
            )
        )

        return (
            source,
            destination,
            btc_amount,
            fee,
            data,
            decoded_tx,
            keyburn,
            is_op_return,
            p2wsh_data,
        )

    else:
        skip_logger.debug("Skipping transaction: {}".format(tx_hash))
        return (None for _ in range(9))


def create_check_hashes(
    db,
    block_index,
    valid_stamps_in_block: list[ValidStamp],
    processed_src20_in_block,
    txhash_list,
    previous_ledger_hash=None,
    previous_txlist_hash=None,
    previous_messages_hash=None,
):
    """
    Calculate and update the hashes for the given block data. This needs to be modified for a reparse.

    Args:
        db (Database): The database object.
        block_index (int): The index of the block.
        valid_stamps_in_block (list): The list of processed transactions in the block.
        processed_src20_in_block (list): The list of valid SRC20 tokens in the block.
        txhash_list (list): The list of transaction hashes in the block.
        previous_ledger_hash (str, optional): The hash of the previous ledger. Defaults to None.
        previous_txlist_hash (str, optional): The hash of the previous transaction list. Defaults to None.
        previous_messages_hash (str, optional): The hash of the previous messages. Defaults to None.

    Returns:
        tuple: A tuple containing the new transaction list hash, ledger hash, and messages hash.
    """
    txlist_content = str(valid_stamps_in_block)
    new_txlist_hash, found_txlist_hash = check.consensus_hash(
        db, block_index, "txlist_hash", previous_txlist_hash, txlist_content
    )

    ledger_content = str(processed_src20_in_block)
    new_ledger_hash, found_ledger_hash = check.consensus_hash(
        db, block_index, "ledger_hash", previous_ledger_hash, ledger_content
    )

    messages_content = str(txhash_list)
    new_messages_hash, found_messages_hash = check.consensus_hash(
        db, block_index, "messages_hash", previous_messages_hash, messages_content
    )

    try:
        update_block_hashes(db, block_index, new_txlist_hash, new_ledger_hash, new_messages_hash)
    except BlockUpdateError as e:
        logger.error(e)
        sys.exit("Exiting due to a critical update error.")

    return new_ledger_hash, new_txlist_hash, new_messages_hash


def commit_and_update_block(db, block_index):
    """
    Commits the changes to the database, updates the parsed block, and increments the block index.

    Args:
        db: The database connection object.
        block_index: The current block index.

    Raises:
        Exception: If an error occurs during the commit or update process.

    Returns:
        None
    """
    try:
        db.commit()
        update_parsed_block(db, block_index)
        block_index += 1
        return block_index
    except Exception as e:
        print("Error message:", e)
        db.rollback()
        db.close()
        sys.exit()


def log_block_info(
    block_index: int,
    start_time: float,
    new_ledger_hash: str,
    new_txlist_hash: str,
    new_messages_hash: str,
    stamps_in_block: int,
    src20_in_block: int,
):
    """
    Logs the information of a block.

    Parameters:
    - block_index (int): The index of the block.
    - start_time (float): The start time of the block.
    - new_ledger_hash (str): The hash of the new ledger.
    - new_txlist_hash (str): The hash of the new transaction list.
    - new_messages_hash (str): The hash of the new messages.

    Returns:
    None
    """
    logger = logging.getLogger(__name__)
    logger.warning(
        "Block: %s (%ss, hashes: L:%s / TX:%s / M:%s / S:%s / S20:%s)"
        % (
            str(block_index),
            "{:.2f}".format(time.time() - start_time),
            new_ledger_hash[-5:] if new_ledger_hash else "N/A",
            new_txlist_hash[-5:],
            new_messages_hash[-5:],
            stamps_in_block,
            src20_in_block,
        )
    )


def process_tx(db, tx_hash, block_index, stamp_issuances, raw_transactions):
    stamp_issuance = filter_issuances_by_tx_hash(stamp_issuances, tx_hash)

    tx_hex = raw_transactions[tx_hash]
    (
        source,
        destination,
        btc_amount,
        fee,
        data,
        decoded_tx,
        keyburn,
        is_op_return,
        p2wsh_data,
    ) = list_tx(db, block_index, tx_hash, tx_hex, stamp_issuance=stamp_issuance)

    return TxResult(
        None,
        source,
        destination,
        btc_amount,
        fee,
        data,
        decoded_tx,
        keyburn,
        is_op_return,
        tx_hash,
        block_index,
        None,
        None,
        p2wsh_data,
    )


def follow(db):
    """
    Continuously follows the blockchain, parsing and indexing new blocks
    for src-20 transactions and to gather details about CP trx such as
    keyburn status.

    Args:
        db: The database connection object.

    Returns:
        None
    """

    # Check software version.
    check.cp_version()  # FIXME: need to add version checks for the endpoints and hash validations
    initialize(db)
    rebuild_balances(db)

    # Get index of last block.
    if util.CURRENT_BLOCK_INDEX == 0:
        logger.warning("New database.")
        block_index = config.BLOCK_FIRST
    else:
        block_index = util.CURRENT_BLOCK_INDEX + 1

    logger.info("Resuming parsing.")
    tx_index = next_tx_index(db)

    # a reorg can happen without the block count increasing, or even for that
    # matter, with the block count decreasing. This should only delay
    # processing of the new blocks a bit.
    try:
        block_tip = backend.getblockcount()
    except (
        ConnectionRefusedError,
        http.client.CannotSendRequest,
        backend.BackendRPCError,
    ) as e:
        if config.FORCE:
            time.sleep(config.BACKEND_POLL_INTERVAL)
        else:
            raise e

    stamp_issuances_list = None
    # profiler = cProfile.Profile()
    # should_profile = True

    while True:
        start_time = time.time()

        try:
            block_tip = backend.getblockcount()
        except (
            ConnectionRefusedError,
            http.client.CannotSendRequest,
            backend.BackendRPCError,
        ) as e:
            if config.FORCE:
                time.sleep(config.BACKEND_POLL_INTERVAL)
                continue
            else:
                raise e

        if block_index != config.BLOCK_FIRST and not is_prev_block_parsed(db, block_index):
            block_index -= 1

        if block_index <= block_tip:
            db.ping()

            if stamp_issuances_list and (stamp_issuances_list[block_index] or stamp_issuances_list[block_index] == []):
                stamp_issuances = stamp_issuances_list[block_index]
            else:
                if block_index + 1 == block_tip:
                    indicator = True
                else:
                    indicator = None
                stamp_issuances_list = fetch_cp_concurrent(block_index, block_tip, indicator=indicator)
                stamp_issuances = stamp_issuances_list[block_index]

            if block_tip - block_index < 100:
                requires_rollback = False
                while True:
                    if block_index == config.BLOCK_FIRST:
                        break
                    logger.info(f"Checking that block {block_index} is not orphan.")
                    current_hash = backend.getblockhash(block_index)
                    current_cblock = backend.getcblock(current_hash)
                    backend_parent = bitcoinlib.core.b2lx(current_cblock.hashPrevBlock)
                    cursor = db.cursor()
                    block_query = """
                    SELECT * FROM blocks WHERE block_index = %s
                    """
                    cursor.execute(block_query, (block_index - 1,))
                    blocks = cursor.fetchall()
                    columns = [desc[0] for desc in cursor.description]
                    cursor.close()
                    blocks_dict = [dict(zip(columns, row)) for row in blocks]
                    if len(blocks_dict) != 1:
                        break
                    db_parent = blocks_dict[0]["block_hash"]

                    if not isinstance(db_parent, str):
                        raise TypeError("db_parent must be a string")
                    if not isinstance(backend_parent, str):
                        raise TypeError("backend_parent must be a string")
                    if db_parent == backend_parent:
                        break
                    else:
                        block_index -= 1
                        requires_rollback = True

                if requires_rollback:
                    logger.warning("Blockchain reorganization at block {}.".format(block_index))
                    block_index -= 1
                    logger.warning("Rolling back to block {} to avoid problems.".format(block_index))
                    purge_block_db(db, block_index)
                    rebuild_balances(db)
                    requires_rollback = False
                    stamp_issuances_list = None
                    continue

            block_hash = backend.getblockhash(block_index)
            cblock = backend.getcblock(block_hash)
            previous_block_hash = bitcoinlib.core.b2lx(cblock.hashPrevBlock)
            block_time = cblock.nTime
            txhash_list, raw_transactions = backend.get_tx_list(cblock)
            util.CURRENT_BLOCK_INDEX = block_index

            try:
                insert_block(
                    db,
                    block_index,
                    block_hash,
                    block_time,
                    previous_block_hash,
                    cblock.difficulty,
                )
            except BlockAlreadyExistsError as e:
                logger.warning(e)
                sys.exit(f"Exiting due to block already existing. {e}")
            except DatabaseInsertError as e:
                logger.error(e)
                sys.exit("Critical database error encountered. Exiting.")

            valid_stamps_in_block: List[ValidStamp] = []

            if not stamp_issuances_list[block_index] and block_index < config.CP_SRC20_GENESIS_BLOCK:
                valid_src20_str = ""
                new_ledger_hash, new_txlist_hash, new_messages_hash = create_check_hashes(
                    db,
                    block_index,
                    valid_stamps_in_block,
                    valid_src20_str,
                    txhash_list,
                )

                stamp_issuances_list.pop(block_index, None)
                log_block_info(
                    block_index,
                    start_time,
                    new_ledger_hash,
                    new_txlist_hash,
                    new_messages_hash,
                    0,
                    0,
                )
                block_index = commit_and_update_block(db, block_index)
                continue

            tx_results = []

            with concurrent.futures.ThreadPoolExecutor() as executor:
                futures = []
                for tx_hash in txhash_list:
                    future = executor.submit(
                        process_tx,
                        db,
                        tx_hash,
                        block_index,
                        stamp_issuances,
                        raw_transactions,
                    )
                    futures.append(future)

                for future in concurrent.futures.as_completed(futures):
                    result = future.result()
                    if result.data is not None:
                        result = result._replace(
                            block_index=block_index,
                            block_hash=block_hash,
                            block_time=block_time,
                        )
                        tx_results.append(result)
                        # tx_index += 1

            tx_results = sorted(tx_results, key=lambda x: txhash_list.index(x.tx_hash))
            # Assign tx_index after sorting
            for i, result in enumerate(tx_results):
                tx_results[i] = result._replace(tx_index=tx_index)
                tx_index += 1

            block_processor = BlockProcessor(db)
            block_processor.insert_transactions(tx_results)
            block_processor.process_transaction_results(tx_results)

            new_ledger_hash, new_txlist_hash, new_messages_hash, stamps_in_block, src20_in_block = (
                block_processor.finalize_block(block_index, block_time, txhash_list)
            )

            stamp_issuances_list.pop(block_index, None)
            log_block_info(
                block_index,
                start_time,
                new_ledger_hash,
                new_txlist_hash,
                new_messages_hash,
                stamps_in_block,
                src20_in_block,
            )
            block_index = commit_and_update_block(db, block_index)

            # if should_profile:
            #     profiler.disable()
            #     profiler.dump_stats("profile_results.prof")
            #     stats = pstats.Stats(profiler).sort_stats('cumulative')
            #     stats.print_stats()
            #     should_profile = False
