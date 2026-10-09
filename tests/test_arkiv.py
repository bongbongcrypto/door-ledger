"""Encoder, helpers, JSON-RPC retries and the signing writer in writer/arkiv.py. Nothing here touches the
network: urlopen is patched and the writer talks to a scripted RPC stub."""
from __future__ import annotations

import email.message
import http.client
import io
import itertools
import json
import math
import unittest
import urllib.error
from unittest import mock

from eth_account import Account
from eth_utils import keccak, to_checksum_address

from writer import arkiv as ak

OWNER = "0x000000000000000000000000000000000000beef"
REGISTRY = "0x4400000000000000000000000000000000000044"


def independent_key(owner: str, nonce: int, salt: int, chain_id: int) -> str:
    """keccak256(uint256 chainId | registry (20 B) | owner (20 B) | uint64 nonce | uint128 salt), as documented
    in the Arkiv node (executor decode), written here without touching writer/arkiv.py."""
    packed = (chain_id.to_bytes(32, "big")
              + bytes.fromhex(REGISTRY[2:])
              + bytes.fromhex(owner[2:].lower().rjust(40, "0"))
              + nonce.to_bytes(8, "big")
              + salt.to_bytes(16, "big"))
    return "0x" + keccak(packed).hex()


def decode_batch(calldata: str) -> list[tuple[int, bytes]]:
    return list(ak.abi_decode(["(uint8,bytes)[]"], bytes.fromhex(calldata[10:]))[0])


def decode_create(data: bytes):
    return ak.abi_decode(["(uint128,uint64,uint64,uint8,(bytes32,uint8,bytes)[])"], data)[0]


class AttributeNameTests(unittest.TestCase):
    def test_valid_names_are_padded_to_32_bytes(self):
        for name in ("app", "t0", "dur_s", "a.b-c", "x" * 32):
            attr = ak.text(name, "v")
            self.assertEqual(attr[0], name.encode().ljust(32, b"\0"))
        # system attributes start with '$' and are written by the payload helper
        self.assertEqual(ak.payload({})[0][0], b"$contentType".ljust(32, b"\0"))

    def test_uppercase_and_malformed_names_are_rejected(self):
        for name in ("App", "KIND", "dur_S", "1abc", "_x", "", "x" * 33, "a b", "café"):
            with self.assertRaises(ValueError, msg=name):
                ak.text(name, "v")

    def test_reserved_words_are_rejected(self):
        for name in ("key", "str", "u64", "u256", "i32", "addr", "bool", "and", "or", "not", "true", "false",
                     "startswith", "exists"):
            with self.assertRaises(ValueError, msg=name):
                ak.u64(name, 1)
        # words the live node accepted as attribute names stay usable
        for name in ("select", "order", "limit"):
            ak.text(name, "v")


class ValueEncodingTests(unittest.TestCase):
    def test_u64_is_one_right_aligned_32_byte_word(self):
        name, type_id, value = ak.u64("t0", 1_791_491_172)
        self.assertEqual(type_id, ak.T_U64)
        self.assertEqual(type_id, 3)
        self.assertEqual(len(value), 32)
        self.assertEqual(value[:24], b"\0" * 24)
        self.assertEqual(int.from_bytes(value, "big"), 1_791_491_172)
        self.assertEqual(len(ak.u64("t0", 2**64 - 1)[2]), 32)
        for bad in (-1, 2**64):
            with self.assertRaises(ValueError):
                ak.u64("t0", bad)

    def test_text_is_raw_utf8(self):
        _, type_id, value = ak.text("route", "SUINEW")
        self.assertEqual((type_id, value), (ak.T_STR, b"SUINEW"))

    def test_payload_is_compact_sorted_json(self):
        ct, body = ak.payload({"b": 1, "a": [1, 2]})
        self.assertEqual(ct, (b"$contentType".ljust(32, b"\0"), ak.T_STR, b"application/json"))
        self.assertEqual(body[1], ak.T_BYTES)
        self.assertEqual(body[2], b'{"a":[1,2],"b":1}')


class OpEncodingTests(unittest.TestCase):
    def test_create_sorts_attributes_ascending(self):
        attrs = [ak.text("venue", "gate"), ak.u64("t0", 5), ak.text("app", "doorledger")] + ak.payload({"v": 1})
        op, salt = ak.op_create(attrs, min_lifetime=900, flags=ak.READONLY, salt=0xD00D)
        self.assertEqual((op.tag, salt), (ak.OP_CREATE, 0xD00D))
        got_salt, expires_at, min_life, flags, enc = decode_create(op.data)
        self.assertEqual((got_salt, expires_at, min_life, flags), (0xD00D, 0, 900, 1))
        names = [a[0] for a in enc]
        self.assertEqual(names, sorted(names))
        self.assertEqual([n.rstrip(b"\0").decode() for n in names],
                         ["$contentType", "$payload", "app", "t0", "venue"])

    def test_duplicate_attribute_names_are_rejected(self):
        with self.assertRaises(ValueError):
            ak.op_create([ak.text("app", "a"), ak.text("app", "b")], min_lifetime=1)

    def test_random_salt_is_128_bit(self):
        _, s1 = ak.op_create([ak.text("app", "a")], min_lifetime=1)
        _, s2 = ak.op_create([ak.text("app", "a")], min_lifetime=1)
        self.assertNotEqual(s1, s2)
        self.assertTrue(0 <= s1 < 2**128)

    def test_extend_transfer_delete_round_trip(self):
        key = independent_key(OWNER, 3, 7, ak.CHAIN_ID)
        ext = ak.op_extend(key, 900)
        self.assertEqual(ext.tag, ak.OP_EXTEND)
        self.assertEqual(ak.abi_decode(["(bytes32,uint64,uint64)"], ext.data)[0], (bytes.fromhex(key[2:]), 0, 900))
        tr = ak.op_transfer(key, ak.BURN.lower())
        self.assertEqual(tr.tag, ak.OP_TRANSFER)
        k, to = ak.abi_decode(["(bytes32,address)"], tr.data)[0]
        self.assertEqual((k, to_checksum_address(to)), (bytes.fromhex(key[2:]), to_checksum_address(ak.BURN)))
        de = ak.op_delete(key)
        self.assertEqual(de.tag, ak.OP_DELETE)
        self.assertEqual(ak.abi_decode(["(bytes32)"], de.data)[0], (bytes.fromhex(key[2:]),))

    def test_op_tags(self):
        self.assertEqual((ak.OP_CREATE, ak.OP_PATCH, ak.OP_EXTEND, ak.OP_TRANSFER, ak.OP_DELETE), (1, 2, 3, 4, 5))
        self.assertEqual((ak.READONLY, ak.PERMISSIONLESS_EXTENSION), (1, 2))


class CalldataTests(unittest.TestCase):
    def test_execute_calldata_selector(self):
        op, _ = ak.op_create([ak.text("app", "a")], min_lifetime=1, salt=1)
        cd = ak.execute_calldata([op])
        self.assertTrue(cd.startswith("0x49650044"))
        self.assertEqual(keccak(text="execute((uint8,bytes)[])")[:4].hex(), "49650044")

    def test_execute_calldata_keeps_op_order(self):
        key = independent_key(OWNER, 0, 1, ak.CHAIN_ID)
        op, _ = ak.op_create([ak.text("app", "a")], min_lifetime=1, salt=1)
        cd = ak.execute_calldata([op, ak.op_transfer(key, ak.BURN), ak.op_delete(key)])
        self.assertEqual([t for t, _ in decode_batch(cd)], [1, 4, 5])
        self.assertEqual(decode_batch(cd)[0][1], op.data)

    def test_empty_batch_is_refused(self):
        with self.assertRaises(ValueError):
            ak.execute_calldata([])

    def test_entity_nonce_selector(self):
        self.assertEqual(ak.SEL_ENTITY_NONCE, keccak(text="entityNonce(address)")[:4])


class EntityKeyTests(unittest.TestCase):
    def test_matches_independent_computation(self):
        cases = [(OWNER, 0, 0xD00D, ak.CHAIN_ID), (OWNER, 0, 0xD00D, 1), (OWNER, 41, 2**128 - 1, ak.CHAIN_ID),
                 ("0x" + "ab" * 20, 2**64 - 1, 0, ak.CHAIN_ID)]
        for owner, nonce, salt, chain in cases:
            self.assertEqual(ak.entity_key(owner, nonce, salt, chain_id=chain),
                             independent_key(owner, nonce, salt, chain))

    def test_owner_case_does_not_change_the_key(self):
        addr = "0x" + "ab" * 20
        self.assertEqual(ak.entity_key(to_checksum_address(addr), 1, 2), ak.entity_key(addr, 1, 2))

    def test_live_vector(self):
        # On Tiramisu, eth_estimateGas of [create(salt=0xD00D), delete(predicted key)] from 0x...beef at entity
        # nonce 0 succeeded only with the chain-7738577 key; the chain-1 key reverted EntityNotFound(0x45e44591...).
        # Prefixes only: full 32-byte values are kept out of the repo.
        self.assertTrue(ak.entity_key(OWNER, 0, 0xD00D).startswith("0xf472ea358b563a37"))
        self.assertTrue(ak.entity_key(OWNER, 0, 0xD00D, chain_id=1).startswith("0x45e445917d01df80"))
        self.assertEqual(ak.CHAIN_ID, 7738577)
        self.assertEqual(ak.REGISTRY.lower(), REGISTRY)


class EventAndErrorTests(unittest.TestCase):
    def test_event_topics_match_the_node(self):
        # first 4 bytes of topic0 as seen in live registry logs
        self.assertTrue(ak.TOPIC_CREATED.startswith("0xb282d7c4"))
        self.assertTrue(ak.TOPIC_EXTENDED.startswith("0x10dc3526"))
        self.assertTrue(ak.TOPIC_TRANSFERRED.startswith("0x0b659dcc"))
        self.assertTrue(ak.TOPIC_DELETED.startswith("0x4059b76c"))
        for t in (ak.TOPIC_CREATED, ak.TOPIC_EXTENDED, ak.TOPIC_TRANSFERRED, ak.TOPIC_DELETED):
            self.assertEqual(len(t), 66)

    def test_revert_data_is_named(self):
        sel = "0x" + keccak(text="NotOwner(bytes32,address,address)")[:4].hex()
        data = sel + "00" * 96
        self.assertEqual(ak.decode_revert(data), "NotOwner")
        msg = ak._explain("eth_estimateGas", {"code": 3, "message": "execution reverted", "data": data})
        self.assertIn("NotOwner", msg)
        self.assertEqual(ak.decode_revert("0xdeadbeef"), "unknown")
        self.assertIn("-32000", ak._explain("eth_call", {"code": -32000, "message": "nope"}))


class RowHelperTests(unittest.TestCase):
    def test_attrs_of_flattens_node_rows(self):
        entity = {"attributes": [{"name": "t0", "type": "u64", "value": "0x6ac7fc64"},
                                 {"name": "venue", "type": "str", "value": "gate"},
                                 {"name": "n", "type": "u64", "value": "12"}]}
        self.assertEqual(ak.attrs_of(entity), {"t0": 0x6ac7fc64, "venue": "gate", "n": 12})
        self.assertEqual(ak.attrs_of({}), {})

    def test_payload_of_decodes_hex_json(self):
        body = json.dumps({"v": 1, "t0": 5}).encode()
        self.assertEqual(ak.payload_of({"payload": "0x" + body.hex()}), {"v": 1, "t0": 5})
        self.assertIsNone(ak.payload_of({}))
        self.assertIsNone(ak.payload_of({"payload": "0x" + b"not json".hex()}))
        self.assertIsNone(ak.payload_of({"payload": "0xzz"}))


def revert_error(sig: str, *words: str) -> ak.ArkivError:
    """What ak.Rpc raises when eth_estimateGas reverts with this custom error (selector + ABI words)."""
    data = "0x" + keccak(text=sig)[:4].hex() + "".join(w[2:].rjust(64, "0") if w.startswith("0x") else w
                                                       for w in words)
    return ak.ArkivError(ak._explain("eth_estimateGas", {"code": 3, "message": "execution reverted", "data": data}))


class OpRefTests(unittest.TestCase):
    def test_ops_name_the_entity_they_touch_in_lower_case(self):
        key = independent_key(OWNER, 3, 7, ak.CHAIN_ID)
        upper = "0x" + key[2:].upper()
        self.assertEqual(ak.op_extend(upper, 900).ref, key)
        self.assertEqual(ak.op_transfer(upper, ak.BURN).ref, key)
        self.assertEqual(ak.op_delete(upper).ref, key)
        op, _ = ak.op_create([ak.text("app", "a")], min_lifetime=1)
        self.assertEqual(op.ref, "")


class DeadKeyTests(unittest.TestCase):
    def test_entity_not_found_and_expired_name_the_key(self):
        key = independent_key(OWNER, 1, 2, ak.CHAIN_ID)
        self.assertEqual(ak.dead_key(revert_error("EntityNotFound(bytes32)", key)), key)
        self.assertEqual(ak.dead_key(revert_error("EntityExpired(bytes32,uint64)", key, "%064x" % 77)), key)
        # a node that answers in upper-case hex
        upper = revert_error("EntityNotFound(bytes32)", "0x" + key[2:].upper())
        self.assertEqual(ak.dead_key(upper), key)

    def test_other_errors_name_no_dead_key(self):
        key = independent_key(OWNER, 1, 2, ak.CHAIN_ID)
        for err in (revert_error("NotOwner(bytes32,address,address)", key, OWNER, OWNER),
                    revert_error("ExpiryNotExtended(bytes32,uint64,uint64)", key, "%064x" % 1, "%064x" % 2),
                    ak.ArkivError("tx 0x%s reverted on chain" % ("ab" * 32)),
                    ak.ArkivError("eth_estimateGas reverted: EntityNotFound (0x1234)"),
                    ak.ArkivError("eth_sendRawTransaction HTTP 503"), ValueError("x")):
            with self.subTest(err=str(err)[:60]):
                self.assertIsNone(ak.dead_key(err))

    def test_pending_is_an_arkiv_error_that_carries_the_hash(self):
        e = ak.ArkivPending("0xabc", "no receipt for 0xabc after 90s")
        self.assertIsInstance(e, ak.ArkivError)
        self.assertEqual((e.tx, str(e)), ("0xabc", "no receipt for 0xabc after 90s"))


class StubRpc:
    """ak.Rpc stand-in: answers[method] is a value, an exception to raise, or a callable(params)."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.calls: list[tuple] = []

    def call(self, method: str, params: list):
        self.calls.append((method, params))
        a = self.answers[method]
        if isinstance(a, Exception):
            raise a
        return a(params) if callable(a) else a

    def methods(self) -> list[str]:
        return [m for m, _ in self.calls]


def throwaway_account():
    # a fixed in-memory test key; nothing signed with it is ever sent anywhere
    return Account.from_key(bytes([0x11]) * 32)


def receipt_json(key: str, status: str = "0x1") -> dict:
    created = {"address": REGISTRY, "topics": [ak.TOPIC_CREATED, key, "0x" + OWNER[2:].rjust(64, "0")],
               "data": "0x" + "%064x" % 9000 + "%064x" % 1}
    foreign = {"address": "0x" + "12" * 20, "topics": [ak.TOPIC_CREATED, "0x" + "%064x" % 5], "data": "0x"}
    return {"status": status, "blockNumber": "0x10", "gasUsed": "0x5208", "logs": [created, foreign]}


class WriterTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(ak.time, "sleep", lambda s: None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.key = independent_key(OWNER, 1, 2, ak.CHAIN_ID)

    def base(self, **over) -> StubRpc:
        answers = {"eth_estimateGas": "0x5208", "eth_getTransactionCount": "0x7", "eth_gasPrice": "0x3b9aca00",
                   "eth_sendRawTransaction": "0x", "eth_getTransactionReceipt": None}
        answers.update(over)
        return StubRpc(answers)

    def test_on_signed_runs_before_the_broadcast_and_a_lost_reply_is_pending(self):
        order = []

        def broadcast(params):
            order.append("broadcast")
            raise ak.ArkivError("eth_sendRawTransaction failed: timed out")
        rpc = self.base(eth_sendRawTransaction=broadcast)
        w = ak.Writer(throwaway_account(), rpc=rpc, log=lambda *_: None)
        with self.assertRaises(ak.ArkivPending) as cm:
            w.send([ak.op_delete(self.key)], wait_s=0, on_signed=lambda h, n: order.append(("signed", h, n)))
        self.assertEqual(len(order), 2)
        (_, h, nonce), second = order
        self.assertEqual((nonce, second), (7, "broadcast"))
        raw = [p for m, p in rpc.calls if m == "eth_sendRawTransaction"][0][0]
        self.assertEqual(h, "0x" + keccak(bytes.fromhex(raw[2:])).hex())  # the hash of what was broadcast
        self.assertEqual(cm.exception.tx, h)
        self.assertIn("broadcast error", str(cm.exception))
        self.assertEqual([p for m, p in rpc.calls if m == "eth_getTransactionCount"][0][1], "pending")

    def test_no_receipt_in_time_is_pending_not_failed(self):
        rpc = self.base()
        w = ak.Writer(throwaway_account(), rpc=rpc, log=lambda *_: None)
        with mock.patch.object(ak.time, "time", side_effect=itertools.chain([0.0, 0.0, 1.0], itertools.repeat(100.0))):
            with self.assertRaises(ak.ArkivPending):
                w.send([ak.op_delete(self.key)], wait_s=5)
        self.assertIn("eth_getTransactionReceipt", rpc.methods())

    def test_mined_receipt_is_returned_with_the_registry_creates(self):
        rpc = self.base(eth_getTransactionReceipt=receipt_json(self.key))
        w = ak.Writer(throwaway_account(), rpc=rpc, log=lambda *_: None)
        signed = []
        rc = w.send([ak.op_delete(self.key)], wait_s=5, on_signed=lambda h, n: signed.append(h))
        self.assertEqual((rc.tx, rc.block, rc.gas_used, rc.created), (signed[0], 16, 21000, [self.key]))

    def test_simulation_revert_raises_before_anything_is_signed(self):
        rpc = self.base(eth_estimateGas=revert_error("EntityNotFound(bytes32)", self.key))
        w = ak.Writer(throwaway_account(), rpc=rpc, log=lambda *_: None)
        signed = []
        with self.assertRaises(ak.ArkivError) as cm:
            w.send([ak.op_extend(self.key, 900)], on_signed=lambda *a: signed.append(a))
        self.assertNotIsInstance(cm.exception, ak.ArkivPending)
        self.assertEqual(signed, [])
        self.assertEqual(ak.dead_key(cm.exception), self.key)
        self.assertNotIn("eth_sendRawTransaction", rpc.methods())

    def test_receipt_known_and_latest_nonce(self):
        w = ak.Writer(throwaway_account(), rpc=self.base(eth_getTransactionReceipt=receipt_json(self.key, "0x0")))
        with self.assertRaises(ak.ArkivError):
            w.receipt("0xabc")                                    # mined and reverted
        w = ak.Writer(throwaway_account(), rpc=self.base(eth_getTransactionReceipt=ak.ArkivError("HTTP 503")))
        with self.assertRaises(ak.ReceiptUnknown):
            w.receipt("0xabc")
        w = ak.Writer(throwaway_account(), rpc=self.base(eth_getTransactionByHash=None))
        self.assertFalse(w.known("0xabc"))
        w = ak.Writer(throwaway_account(), rpc=self.base(eth_getTransactionByHash={"hash": "0xabc"}))
        self.assertTrue(w.known("0xabc"))
        w = ak.Writer(throwaway_account(), rpc=self.base(eth_getTransactionByHash=ak.ArkivError("HTTP 503")))
        self.assertTrue(w.known("0xabc"))                         # unsure counts as known: keep waiting
        rpc = self.base(eth_getTransactionCount="0x2a")
        w = ak.Writer(throwaway_account(), rpc=rpc)
        self.assertEqual(w.nonce_latest(), 42)
        self.assertEqual(rpc.calls[-1], ("eth_getTransactionCount", [w.addr, "latest"]))


class FakeHTTPResponse:
    def __init__(self, result: str = "0x10"):
        self.body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}).encode()

    def read(self, *a) -> bytes:
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code: int, retry_after: str | None = None) -> urllib.error.HTTPError:
    hdrs = email.message.Message()
    if retry_after is not None:
        hdrs["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://rpc.invalid", code, "status %d" % code, hdrs, io.BytesIO(b""))


class RpcRetryTests(unittest.TestCase):
    def call(self, effects, retries: int = 4):
        sleeps = []
        with mock.patch.object(ak.urllib.request, "urlopen", side_effect=effects) as op, \
                mock.patch.object(ak.time, "sleep", side_effect=sleeps.append):
            try:
                out = ak.Rpc(url="https://rpc.invalid", retries=retries).call("eth_blockNumber", [])
            except Exception as e:  # noqa: BLE001 - returned for the assertion
                out = e
        return out, sleeps, op.call_count

    def test_transient_socket_and_http_protocol_errors_are_retried(self):
        out, sleeps, n = self.call([ConnectionResetError("reset"), http.client.RemoteDisconnected("closed"),
                                    http.client.IncompleteRead(b"par"), TimeoutError("slow"), FakeHTTPResponse()])
        self.assertEqual((out, sleeps, n), ("0x10", [2.0, 4.0, 8.0, 16.0], 5))

    def test_exhausted_retries_raise_arkiv_error(self):
        out, sleeps, n = self.call([OSError("network down")] * 5)
        self.assertIsInstance(out, ak.ArkivError)
        self.assertEqual((len(sleeps), n), (4, 5))
        out, _, _ = self.call([http.client.BadStatusLine("garbage")] * 5)
        self.assertIsInstance(out, ak.ArkivError)

    def test_retry_after_seconds_are_honoured_and_capped(self):
        out, sleeps, _ = self.call([http_error(429, "3"), FakeHTTPResponse()])
        self.assertEqual((out, sleeps), ("0x10", [3.0]))
        out, sleeps, _ = self.call([http_error(503, "86400"), FakeHTTPResponse()])
        self.assertEqual((out, sleeps), ("0x10", [120.0]))
        out, sleeps, _ = self.call([http_error(502), FakeHTTPResponse()])
        self.assertEqual((out, sleeps), ("0x10", [2.0]))

    def test_retry_after_http_date_falls_back_to_backoff(self):
        out, sleeps, _ = self.call([http_error(429, "Wed, 21 Oct 2026 07:28:00 GMT"), http_error(429, ""),
                                    FakeHTTPResponse()])
        self.assertEqual((out, sleeps), ("0x10", [2.0, 4.0]))

    def test_retry_after_out_of_range_values_give_a_valid_wait(self):
        # time.sleep raises ValueError on a negative or NaN wait, so whatever the header says the wait must be a
        # finite number in [0, 120] and the call must still succeed.
        for value in ("-5", "nan", "inf", "-inf", "1e400"):
            with self.subTest(retry_after=value):
                out, sleeps, _ = self.call([http_error(429, value), FakeHTTPResponse()])
                self.assertEqual(out, "0x10")
                self.assertEqual(len(sleeps), 1)
                self.assertTrue(math.isfinite(sleeps[0]) and 0 <= sleeps[0] <= 120, sleeps)

    def test_client_errors_and_rpc_errors_are_not_retried(self):
        out, sleeps, n = self.call([http_error(400)])
        self.assertIsInstance(out, ak.ArkivError)
        self.assertEqual((sleeps, n), ([], 1))
        err = FakeHTTPResponse()
        err.body = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000, "message": "nonce too low"}}).encode()
        out, sleeps, n = self.call([err])
        self.assertIsInstance(out, ak.ArkivError)
        self.assertIn("nonce too low", str(out))
        self.assertEqual((sleeps, n), ([], 1))


if __name__ == "__main__":
    unittest.main()
