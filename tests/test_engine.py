"""Reporter state machine (writer/engine.py) against scripted venue polls and an in-memory registry.

FakeWriter stands in for ak.Writer. It encodes every batch with ak.execute_calldata (the bytes a real
send would carry), decodes the ops back out of that calldata, simulates them, reports the transaction
hash through on_signed before "broadcasting", and applies them to FakeRegistry, which follows the
registry rules this code depends on: entity key = keccak(chainId | registry | owner | entity nonce |
salt) with one nonce per create, expiry = max(expiresAt, block + minLifetime), owner checks, the
permissionless-extension flag, sorted attributes, one 32-byte word per u64, reverts that carry the
registry's custom-error data, and receipts and eth_getLogs entries that carry the registry's event
topics. A transaction's fate after signing is scripted (mined, mined but unheard, stuck, dropped,
replaced, reverted). Nothing is signed and nothing leaves the process.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from eth_utils import keccak, to_checksum_address

from writer import arkiv as ak
from writer import engine
from writer import venues as vn

ME = to_checksum_address("0x000000000000000000000000000000000000beef")
REGISTRY = "0x4400000000000000000000000000000000000044"
START_T = 1_800_000_000
START_HEAD = 5_000
ROUTE = "XNET"
NO_PULSES = 10**12                         # a pulse interval no test clock reaches
HALT_ID = ("gate", ROUTE, "deposit")
BIN_ID = ("binance", ROUTE, "deposit")
ASSETS = ("AAA", "BBB", "CCC", "DDD")      # tradable on XNET, the route that halts
OTHER = ("EEE", "FFF", "GGG")              # tradable on YNET, which stays open

# poll states
HALT, OPEN, FAIL, SHORT_HALT, SHORT_OPEN, GONE = "halt", "open", "fail", "short-halt", "short-open", "gone"


# ---------------------------------------------------------------------------------------------
# Calldata decoding (independent of writer/arkiv.py's encoders)
# ---------------------------------------------------------------------------------------------

def decode_ops(calldata: str) -> list[dict]:
    if not calldata.startswith("0x49650044"):
        raise AssertionError("not an execute() call: %s" % calldata[:10])
    (items,) = ak.abi_decode(["(uint8,bytes)[]"], bytes.fromhex(calldata[10:]))
    out = []
    for tag, data in items:
        if tag == 1:
            salt, expires_at, life, flags, raw = ak.abi_decode(
                ["(uint128,uint64,uint64,uint8,(bytes32,uint8,bytes)[])"], data)[0]
            attrs, payload = {}, None
            for name, type_id, value in raw:
                n = name.rstrip(b"\0").decode()
                if n == "$payload":
                    payload = json.loads(value)
                elif type_id == ak.T_U64:
                    attrs[n] = int.from_bytes(value, "big")
                elif type_id == ak.T_STR:
                    attrs[n] = value.decode()
                else:
                    attrs[n] = value
            out.append({"op": "create", "salt": salt, "expires_at": expires_at, "min_lifetime": life,
                        "flags": flags, "attrs": attrs, "payload": payload, "raw": list(raw)})
        elif tag == 3:
            key, expires_at, life = ak.abi_decode(["(bytes32,uint64,uint64)"], data)[0]
            out.append({"op": "extend", "key": "0x" + key.hex(), "expires_at": expires_at, "min_lifetime": life})
        elif tag == 4:
            key, to = ak.abi_decode(["(bytes32,address)"], data)[0]
            out.append({"op": "transfer", "key": "0x" + key.hex(), "to": to_checksum_address(to)})
        elif tag == 5:
            (key,) = ak.abi_decode(["(bytes32)"], data)[0]
            out.append({"op": "delete", "key": "0x" + key.hex()})
        else:
            out.append({"op": "tag%d" % tag})
    return out


def derive_key(owner: str, nonce: int, salt: int) -> str:
    packed = (ak.CHAIN_ID.to_bytes(32, "big") + bytes.fromhex(REGISTRY[2:]) + bytes.fromhex(owner[2:].lower())
              + nonce.to_bytes(8, "big") + salt.to_bytes(16, "big"))
    return "0x" + keccak(packed).hex()


def word(n: int) -> str:
    return "%064x" % n


def addr_topic(addr: str) -> str:
    return "0x" + addr[2:].lower().rjust(64, "0")


REVERT_SIGS = {
    "AttributesNotSorted": "AttributesNotSorted()", "EmptyBatch": "EmptyBatch()",
    "EntityExpired": "EntityExpired(bytes32,uint64)", "EntityNotFound": "EntityNotFound(bytes32)",
    "ExpiryNotExtended": "ExpiryNotExtended(bytes32,uint64,uint64)", "InvalidOpType": "InvalidOpType(uint8)",
    "InvalidValueType": "InvalidValueType(bytes32,uint8)", "NotOwner": "NotOwner(bytes32,address,address)",
    "ReservedCreationFlags": "ReservedCreationFlags(uint8)", "TransferToSelf": "TransferToSelf(bytes32)",
    "TransferToZeroAddress": "TransferToZeroAddress(bytes32)",
}


def revert(name: str, *args) -> ak.ArkivError:
    """The error ak.Rpc raises when eth_estimateGas hits a registry revert: the node's revert data (selector +
    one ABI word per argument), named by writer/arkiv.py's own _explain, exactly as a real simulation reports it."""
    data = "0x" + keccak(text=REVERT_SIGS[name])[:4].hex()
    for a in args:
        data += a[2:].lower().rjust(64, "0") if isinstance(a, str) else word(a)
    return ak.ArkivError(ak._explain("eth_estimateGas", {"code": 3, "message": "execution reverted", "data": data}))


# ---------------------------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------------------------

class FakeRegistry:
    """The ak.Rpc surface the Reporter reads (block_number, entity_nonce, query, call('eth_getLogs')) plus
    registry state and the registry's event log."""

    def __init__(self, head: int = START_HEAD):
        self.head = head
        self.entities: dict[str, dict] = {}
        self.nonces: dict[str, int] = {}
        self.txs = 0
        self.queries: list[str] = []
        self.calls: list[tuple] = []
        self.log_history: list[dict] = []
        self.fail_methods: set[str] = set()
        self.stale_nonce_reads = 0             # entity_nonce answers this many reads one behind (a lagging node)

    def block_number(self) -> int:
        return self.head

    def entity_nonce(self, owner: str) -> int:
        n = self.nonces.get(owner.lower(), 0)
        if self.stale_nonce_reads and n:
            self.stale_nonce_reads -= 1
            return n - 1
        return n

    def call(self, method: str, params: list):
        self.calls.append((method, params))
        if method in self.fail_methods:
            raise ak.ArkivError("%s HTTP 503" % method)
        if method != "eth_getLogs":
            raise AssertionError("fake rpc does not model %s" % method)
        f = params[0]
        lo, hi = int(f["fromBlock"], 16), int(f["toBlock"], 16)
        want = [t.lower() if t else None for t in f.get("topics", [])]
        out = []
        for lg in self.log_history:
            if lg["address"].lower() != f["address"].lower() or not lo <= int(lg["blockNumber"], 16) <= hi:
                continue
            if any(t and (i >= len(lg["topics"]) or lg["topics"][i].lower() != t) for i, t in enumerate(want)):
                continue
            out.append(dict(lg))
        return out

    def query(self, q: str, at_block=None, limit: int = 100, cursor=None, select=None) -> dict:
        """Models `name = str(..)` / `$owner = addr(..)` clauses joined by AND, and one level of
        parenthesised OR."""
        self.queries.append(q)
        groups = []
        for part in q.split(" AND "):
            part = part.strip()
            alts = part[1:-1].split(" OR ") if part.startswith("(") and part.endswith(")") else [part]
            group = []
            for alt in alts:
                m = re.fullmatch(r"\s*(\$?[A-Za-z][\w.-]*) = (str|addr)\((.*)\)\s*", alt)
                if not m:
                    raise AssertionError("query clause the fake does not model: %r" % alt)
                group.append((m.group(1), m.group(3).strip("'")))
            groups.append(group)
        rows = []
        for key, e in self.entities.items():
            if e["expires"] <= self.head:        # reads hide expired rows
                continue
            if all(any(self._match(e, n, v) for n, v in g) for g in groups):
                rows.append(self._row(key, e))
        # wire shape of arkiv_query: {"data": [...], "blockNumber", "cursor"}
        return {"data": rows[:limit], "blockNumber": hex(self.head), "cursor": None}

    @staticmethod
    def _match(e: dict, name: str, value: str) -> bool:
        if name == "$owner":
            return e["owner"] == value.lower()
        if name == "$creator":
            return e["creator"] == value.lower()
        return e["attrs"].get(name, (None, None))[1] == value

    @staticmethod
    def _row(key: str, e: dict) -> dict:
        attrs = [{"name": n, "type": "u64" if t == ak.T_U64 else "str", "value": hex(v) if t == ak.T_U64 else v}
                 for n, (t, v) in sorted(e["attrs"].items()) if not n.startswith("$")]
        return {"key": key, "owner": e["owner"], "creator": e["creator"], "createdAt": hex(e["created"]),
                "expiresAt": hex(e["expires"]), "attributes": attrs, "payload": "0x" + e["payload"].hex()}

    def apply(self, sender: str, ops: list[dict], tx: str | None = None, dry_run: bool = False):
        """One transaction in the next block: all ops or none (an error leaves state untouched). With dry_run
        nothing is committed (eth_estimateGas)."""
        sender = sender.lower()
        block = self.head + 1
        staged = {k: dict(v) for k, v in self.entities.items()}
        nonce = self.nonces.get(sender, 0)
        logs = []

        def live(key):
            e = staged.get(key)
            if e is None:
                raise revert("EntityNotFound", key)
            if e["expires"] <= block:
                raise revert("EntityExpired", key, e["expires"])
            return e

        if not ops:
            raise revert("EmptyBatch")
        for op in ops:
            kind = op["op"]
            if kind == "create":
                names = [a[0] for a in op["raw"]]
                if names != sorted(names) or len(set(names)) != len(names):
                    raise revert("AttributesNotSorted")
                if op["flags"] & ~(ak.READONLY | ak.PERMISSIONLESS_EXTENSION):
                    raise revert("ReservedCreationFlags", op["flags"])
                attrs, payload = {}, b""
                for name, type_id, value in op["raw"]:
                    n = name.rstrip(b"\0").decode()
                    if type_id == ak.T_U64:
                        if len(value) != 32:
                            raise revert("InvalidValueType", "0x" + name.hex(), type_id)
                        attrs[n] = (type_id, int.from_bytes(value, "big"))
                    elif n == "$payload":
                        payload = value
                    else:
                        attrs[n] = (type_id, value.decode())
                key = derive_key(sender, nonce, op["salt"])
                nonce += 1
                expires = max(op["expires_at"], block + op["min_lifetime"])
                staged[key] = {"owner": sender, "creator": sender, "flags": op["flags"], "attrs": attrs,
                               "payload": payload, "created": block, "expires": expires}
                logs.append({"address": REGISTRY, "topics": [ak.TOPIC_CREATED, key, addr_topic(sender)],
                             "data": "0x" + word(expires) + word(op["flags"])})
            elif kind == "extend":
                e = live(op["key"])
                if e["owner"] != sender and not e["flags"] & ak.PERMISSIONLESS_EXTENSION:
                    raise revert("NotOwner", op["key"], sender, e["owner"])
                new = max(op["expires_at"], block + op["min_lifetime"])
                if new <= e["expires"]:
                    raise revert("ExpiryNotExtended", op["key"], e["expires"], new)
                e["expires"] = new
                logs.append({"address": REGISTRY, "topics": [ak.TOPIC_EXTENDED, op["key"], addr_topic(e["owner"])],
                             "data": "0x" + word(new)})
            elif kind == "transfer":
                e = live(op["key"])
                if e["owner"] != sender:
                    raise revert("NotOwner", op["key"], sender, e["owner"])
                if int(op["to"], 16) == 0:
                    raise revert("TransferToZeroAddress", op["key"])
                if op["to"].lower() == sender:
                    raise revert("TransferToSelf", op["key"])
                prev, e["owner"] = e["owner"], op["to"].lower()
                logs.append({"address": REGISTRY, "topics": [ak.TOPIC_TRANSFERRED, op["key"], addr_topic(prev),
                                                             addr_topic(op["to"])], "data": "0x"})
            elif kind == "delete":
                e = live(op["key"])
                if e["owner"] != sender:
                    raise revert("NotOwner", op["key"], sender, e["owner"])
                del staged[op["key"]]
                logs.append({"address": REGISTRY, "topics": [ak.TOPIC_DELETED, op["key"], addr_topic(sender)],
                             "data": "0x"})
            else:
                raise revert("InvalidOpType", 0)
        if dry_run:
            return None
        self.txs += 1
        tx = tx or "0x" + word(0xFEED0000 + self.txs)
        for lg in logs:
            lg["blockNumber"], lg["transactionHash"] = hex(block), tx
        self.log_history += logs
        self.entities, self.nonces[sender], self.head = staged, nonce, block
        created = [lg["topics"][1] for lg in logs if lg["topics"][0] == ak.TOPIC_CREATED]
        return ak.Receipt(tx, block, 21_000 + 90_000 * len(ops), created, logs)


class FakeWriter:
    """Same surface as ak.Writer for the Reporter: addr, rpc, send(ops, on_signed), receipt(h), nonce_latest(),
    known(h).

    send() follows ak.Writer.send: the batch is simulated first (a registry revert raises ArkivError before
    anything is signed), then signed (on_signed(tx_hash, nonce) runs before the broadcast), then broadcast.
    `outcomes` scripts what happens to each signed transaction, in order (default "ok"):
      ok        mined; the receipt is returned
      lost      mined, but the sender never hears back (ArkivPending)
      stuck     the node holds it unmined (ArkivPending); mine_stuck() mines it later
      dropped   the node lost it (ArkivPending); the hash is unknown and the nonce does not move
      replaced  another transaction with the same nonce was mined instead (ArkivPending)
      reverted  mined and reverted on chain (ArkivError, as ak.Writer.wait raises it)
    """

    def __init__(self, registry: FakeRegistry, addr: str = ME):
        self.rpc = registry
        self.addr = addr
        self.batches: list[list[dict]] = []    # decoded ops of every batch that landed
        self.attempts = 0                      # send() calls
        self.fail_next = 0                     # RPC failures before signing (nothing signed, nothing sent)
        self.outcomes: list[str] = []
        self.signed: list[tuple] = []          # (tx hash, nonce, decoded ops) of every signed transaction
        self.receipts: dict[str, ak.Receipt] = {}
        self.mempool: dict[str, list] = {}
        self.reverted: set[str] = set()
        self.nonce = 0                         # mined transactions from this wallet (the "latest" count)
        self.on_broadcast = None               # test hook between on_signed and the broadcast

    def send(self, ops, wait_s: float = 90.0, on_signed=None) -> ak.Receipt:
        self.attempts += 1
        calldata = ak.execute_calldata(ops)
        decoded = decode_ops(calldata)
        if self.fail_next:
            self.fail_next -= 1
            raise ak.ArkivError("eth_estimateGas HTTP 503")
        self.rpc.apply(self.addr, decoded, dry_run=True)
        nonce = self.nonce + len(self.mempool)
        h = "0x" + keccak(bytes.fromhex(calldata[2:]) + nonce.to_bytes(8, "big")).hex()
        self.signed.append((h, nonce, decoded))
        if on_signed:
            on_signed(h, nonce)
        if self.on_broadcast:
            self.on_broadcast(h, nonce)
        outcome = self.outcomes.pop(0) if self.outcomes else "ok"
        if outcome == "ok":
            return self._mine(h, decoded)
        if outcome == "lost":
            self._mine(h, decoded)
        elif outcome == "stuck":
            self.mempool[h] = decoded
        elif outcome == "replaced":
            self.nonce += 1
        elif outcome == "reverted":
            self.nonce += 1
            self.reverted.add(h)
            raise ak.ArkivError("tx %s reverted on chain" % h)
        elif outcome != "dropped":
            raise AssertionError("unknown outcome %r" % outcome)
        raise ak.ArkivPending(h, "no receipt for %s after %ss (%s)" % (h, wait_s, outcome))

    def _mine(self, h: str, decoded: list) -> ak.Receipt:
        rc = self.rpc.apply(self.addr, decoded, tx=h)
        self.nonce += 1
        self.receipts[h] = rc
        self.batches.append(decoded)
        return rc

    def mine_stuck(self) -> None:
        for h, decoded in list(self.mempool.items()):
            del self.mempool[h]
            try:
                self._mine(h, decoded)
            except ak.ArkivError:
                self.nonce += 1
                self.reverted.add(h)

    def receipt(self, h: str):
        if h in self.reverted:
            raise ak.ArkivError("tx %s reverted on chain" % h)
        return self.receipts.get(h)

    def nonce_latest(self) -> int:
        return self.nonce

    def known(self, h: str) -> bool:
        return h in self.receipts or h in self.mempool or h in self.reverted


def contract(asset: str) -> str:
    return "0x" + asset.encode().hex().rjust(40, "0")


def snap_of(venue: str, t: int, xnet: dict, items: int = 100, extra_tradable: int = 0) -> vn.Snapshot:
    """XNET carries the assets in `xnet` (asset -> deposit switch, withdraw always open); YNET carries OTHER,
    all open. `extra_tradable` adds tradable coins with no rows (they only grow the tradable list)."""
    rows = [vn.Row(a, ROUTE, contract(a), dep, True) for a, dep in xnet.items()]
    rows += [vn.Row(a, "YNET", contract(a), True, True) for a in OTHER]
    filler = {"Z%04d" % i for i in range(extra_tradable)}
    return vn.Snapshot(venue, t, True, rows, set(xnet) | set(OTHER) | filler, items, "resp-%d" % t, 4096)


def make_snap(venue: str, t: int, state: str, items: int = 100, extra_tradable: int = 0) -> vn.Snapshot:
    if state == FAIL:
        return vn.Snapshot(venue, t, False, error="URLError: timed out")
    if state == GONE:                      # XNET is missing from the response: the route cannot be judged
        return snap_of(venue, t, {}, items, extra_tradable)
    halted = state in (HALT, SHORT_HALT)
    n = int(items * 0.8) if state in (SHORT_HALT, SHORT_OPEN) else items
    return snap_of(venue, t, {a: not halted for a in ASSETS}, n, extra_tradable)


class Clock:
    def __init__(self, t: int):
        self.t = t

    def __call__(self) -> int:
        return self.t


class Harness:
    def __init__(self, venues=("gate",), pulses: bool = False, state_path: str | None = None,
                 lease_life: int = 900, pulse_every_s: int = 3600, pulse_life: int = 4500, sampled: bool = False):
        self.clock = Clock(START_T)
        self.reg = FakeRegistry()
        self.w = FakeWriter(self.reg)
        self.states: dict[str, object] = {}
        self.logs: list[str] = []
        self.items = 100
        self.extra_tradable = 0
        self.kw = dict(venues=venues, lease_life=lease_life, pulse_life=pulse_life, sampled=sampled,
                       pulse_every_s=pulse_every_s if pulses else NO_PULSES)
        self.rep = self.new_reporter(state_path)

    def new_reporter(self, state_path: str | None = None, **kw) -> engine.Reporter:
        rep = engine.Reporter(self.w, state_path=state_path, poll=self.poll, now=self.clock, log=self.logs.append,
                              **{**self.kw, **kw})
        self.rep = rep
        return rep

    def poll(self, venue: str) -> vn.Snapshot:
        s = self.states.get(venue, OPEN)
        if isinstance(s, dict):
            return snap_of(venue, self.clock.t, s, self.items, self.extra_tradable)
        return make_snap(venue, self.clock.t, s, self.items, self.extra_tradable)

    def step(self, state, dt: int = 120, blocks: int = 60, **per_venue) -> list[list[dict]]:
        """Advance the clock (dt s) and the chain (blocks), run one full cycle, return the batches it landed.
        `state` is a poll state name or a dict asset -> deposit switch for XNET."""
        self.clock.t += dt
        self.reg.head += blocks
        for v in self.rep.venues:
            self.states[v] = per_venue.get(v, state)
        n = len(self.w.batches)
        self.rep.cycle()
        return self.w.batches[n:]

    def created(self, kind: str) -> list[dict]:
        """Every create of this kind that ever landed (live, expired or deleted)."""
        return [op for b in self.w.batches for op in b if op["op"] == "create" and op["attrs"].get("kind") == kind]

    def live(self, kind: str) -> dict:
        return {k: e for k, e in self.reg.entities.items()
                if e["attrs"]["kind"][1] == kind and e["expires"] > self.reg.head}


def kinds(batch: list[dict]) -> list[str]:
    return [o["op"] for o in batch]


class EngineCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"DOORLEDGER_CODE": "testcode"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def open_lease(self, h: Harness) -> int:
        """Two halted polls; returns the first poll's time (the halt's t0)."""
        self.assertEqual(h.step(HALT), [])
        t0 = h.clock.t
        sent = h.step(HALT)
        self.assertEqual([kinds(b) for b in sent], [["create"]])
        return t0


# ---------------------------------------------------------------------------------------------
# Opening
# ---------------------------------------------------------------------------------------------

class OpenTests(EngineCase):
    def test_halt_seen_once_writes_nothing(self):
        h = Harness()
        self.assertEqual(h.step(HALT), [])
        self.assertEqual(h.rep.opens, {})
        self.assertEqual(h.w.attempts, 0)
        self.assertIn(HALT_ID, h.rep.closed_streak)
        # an open poll in between restarts the count
        self.assertEqual(h.step(OPEN), [])
        self.assertEqual(h.rep.closed_streak, {})
        self.assertEqual(h.step(HALT), [])
        self.assertEqual(h.w.attempts, 0)

    def test_halt_seen_twice_opens_one_lease_with_first_poll_time(self):
        h = Harness()
        h.step(HALT)
        t_first = h.clock.t
        sent = h.step(HALT)
        self.assertEqual(len(sent), 1)
        self.assertEqual(kinds(sent[0]), ["create"])
        c = sent[0][0]
        a, p = c["attrs"], c["payload"]
        self.assertEqual((a["app"], a["kind"], a["venue"], a["route"], a["net"], a["side"]),
                         ("doorledger", "lease", "gate", ROUTE, "xnet", "deposit"))
        self.assertEqual(a["t0"], t_first)
        self.assertEqual(a["$contentType"], "application/json")
        self.assertEqual((c["flags"], c["min_lifetime"], c["expires_at"]), (ak.READONLY, 900, 0))
        self.assertEqual(p["t0"], t_first)
        self.assertEqual((p["listed"], p["closed"], p["assets"]), (4, 4, list(ASSETS)))
        self.assertEqual((p["code"], p["src"], p["coverage"]), ("testcode", vn.URLS["gate"][0], "continuous"))
        self.assertEqual(p["eta_unix_ms"], 0)
        self.assertNotIn("eta_ms", p)
        self.assertEqual(p["rule"], {"min_tradable": vn.MIN_ASSETS, "close_share": vn.CLOSE_SHARE,
                                     "open_line": vn.OPEN_LINE, "open_polls": engine.OPEN_POLLS,
                                     "close_polls": engine.CLOSE_POLLS})
        self.assertEqual(p["resp_sha256"], "resp-%d" % h.clock.t)
        self.assertNotIn("resumed_from", p)
        # the reporter tracks the key the registry actually created
        o = h.rep.opens[HALT_ID]
        e = h.reg.entities[o.lease_key]
        self.assertEqual((e["owner"], e["creator"]), (ME.lower(), ME.lower()))
        self.assertEqual(e["attrs"]["kind"], (ak.T_STR, "lease"))
        self.assertEqual(o.expires_at, e["expires"])
        self.assertEqual(e["expires"], h.reg.head + 900)
        self.assertEqual(o.t0, t_first)
        self.assertEqual(list(h.rep.opens), [HALT_ID])      # the open withdraw side is not a halt
        # still halted: no second lease
        self.assertEqual(h.step(HALT), [])
        self.assertEqual(len([e for e in h.reg.entities.values() if e["attrs"]["kind"][1] == "lease"]), 1)

    def test_heartbeat_extends_only_inside_renew_window(self):
        h = Harness()
        self.open_lease(h)
        key = h.rep.opens[HALT_ID].lease_key
        extends = quiet = 0
        for _ in range(16):
            expires = h.reg.entities[key]["expires"]
            head = h.reg.head + 60                   # the head the next cycle reads
            remaining = expires - head
            sent = h.step(HALT)
            if 5 < remaining < 900 * 2 // 3:
                self.assertEqual([kinds(b) for b in sent], [["extend"]], "remaining %d" % remaining)
                ext = sent[0][0]
                self.assertEqual((ext["key"], ext["min_lifetime"], ext["expires_at"]), (key, 900, 0))
                self.assertEqual(h.reg.entities[key]["expires"], h.reg.head + 900)
                self.assertEqual(h.rep.opens[HALT_ID].expires_at, h.reg.entities[key]["expires"])
                extends += 1
            else:
                self.assertEqual(sent, [], "remaining %d" % remaining)
                quiet += 1
        self.assertGreaterEqual(extends, 2)
        self.assertGreater(quiet, extends)
        self.assertEqual(h.rep.opens[HALT_ID].lease_key, key)

    def test_heartbeat_stops_while_the_venue_has_been_unreadable_past_stale_s(self):
        h = Harness()
        self.open_lease(h)
        o = h.rep.opens[HALT_ID]
        key = o.lease_key
        expires = h.reg.entities[key]["expires"]
        # 10 unreadable polls, 10 minutes apart; the lease enters its renew window only after the halt has not
        # been seen for STALE_S, so it must not be renewed
        for _ in range(10):
            self.assertEqual(h.step(FAIL, dt=600, blocks=50), [])
        self.assertGreaterEqual(h.clock.t - o.seen_at, engine.STALE_S)
        self.assertLess(expires - h.reg.head, 600)
        self.assertGreater(expires - h.reg.head, 5)
        # the venue is back and still halted: renewed in the same cycle
        sent = h.step(HALT, dt=120, blocks=10)
        self.assertEqual([kinds(b) for b in sent], [["extend"]])

    def test_short_response_is_ignored(self):
        h = Harness()
        self.assertEqual(h.step(OPEN), [])                  # sets the venue's max item count (100)
        self.assertEqual(h.step(SHORT_HALT), [])            # 80 items < 90% of 100
        self.assertEqual(h.rep.tick["gate"]["fail"], 1)
        self.assertNotIn(HALT_ID, h.rep.closed_streak)
        self.assertEqual(h.step(HALT), [])                  # had the short poll counted, this would open
        t_first = h.clock.t
        sent = h.step(HALT)
        self.assertEqual([kinds(b) for b in sent], [["create"]])
        self.assertEqual(sent[0][0]["attrs"]["t0"], t_first)
        # on the closing side too: a short open poll is not one of the three
        self.assertEqual(h.step(OPEN), [])
        self.assertEqual(h.step(SHORT_OPEN), [])
        self.assertEqual(h.step(OPEN), [])
        self.assertIn(HALT_ID, h.rep.opens)
        self.assertEqual([kinds(b) for b in h.step(OPEN)], [["create", "transfer", "delete"]])


# ---------------------------------------------------------------------------------------------
# Closing
# ---------------------------------------------------------------------------------------------

class CloseTests(EngineCase):
    def test_three_judged_open_polls_close_in_one_batch(self):
        h = Harness()
        t0 = self.open_lease(h)
        lease = h.rep.opens[HALT_ID]
        lease_key, lease_tx = lease.lease_key, lease.lease_tx
        self.assertEqual(h.step(OPEN), [])
        t1 = h.clock.t
        self.assertEqual(h.step(OPEN), [])
        sent = h.step(OPEN)
        self.assertEqual(len(sent), 1)
        self.assertEqual(kinds(sent[0]), ["create", "transfer", "delete"])
        ep, tr, de = sent[0]
        self.assertEqual(ep["flags"], ak.READONLY | ak.PERMISSIONLESS_EXTENSION)
        self.assertEqual(ep["flags"], 3)
        self.assertEqual(ep["min_lifetime"], 180 * 86_400 // 2)     # 180 days of 2 s blocks
        a, p = ep["attrs"], ep["payload"]
        self.assertEqual((a["kind"], a["venue"], a["route"], a["side"]), ("episode", "gate", ROUTE, "deposit"))
        self.assertEqual((a["t0"], a["t1"], a["dur_s"]), (t0, t1, t1 - t0))
        self.assertEqual((p["t0"], p["t1"], p["dur_s"]), (t0, t1, t1 - t0))
        self.assertEqual((p["lease_key"], p["lease_tx"], p["gaps"], p["polls"]), (lease_key, lease_tx, [], 2))
        self.assertEqual((p["listed"], p["max_closed"], p["coverage"]), (4, 4, "continuous"))
        self.assertEqual(lease_tx, h.w.signed[0][0])        # the hash on_signed reported for the lease batch
        # transfer moves the episode just created; delete removes the lease
        episodes = [k for k, e in h.reg.entities.items() if e["attrs"]["kind"][1] == "episode"]
        self.assertEqual(len(episodes), 1)
        self.assertEqual(tr["key"], episodes[0])
        self.assertEqual(tr["to"], to_checksum_address(ak.BURN))
        self.assertEqual(de["key"], lease_key)
        e = h.reg.entities[episodes[0]]
        self.assertEqual((e["owner"], e["creator"], e["flags"]), (ak.BURN.lower(), ME.lower(), 3))
        self.assertEqual(e["expires"], h.reg.head + engine.EPISODE_LIFE)
        self.assertNotIn(lease_key, h.reg.entities)
        self.assertEqual(h.rep.opens, {})
        self.assertEqual(h.rep.open_streak, {})
        self.assertEqual(h.step(OPEN), [])

    def test_venue_failure_between_open_polls_does_not_close_and_is_recorded_as_gap(self):
        h = Harness()
        t0 = self.open_lease(h)
        self.assertEqual(h.step(OPEN), [])
        t1 = h.clock.t
        self.assertEqual(h.step(FAIL), [])
        t_fail = h.clock.t
        self.assertEqual(h.rep.opens[HALT_ID].gap_since, t_fail)
        self.assertEqual(h.step(OPEN), [])                  # only 2 judged open polls so far
        t_back = h.clock.t
        self.assertIn(HALT_ID, h.rep.opens)
        self.assertEqual(h.rep.opens[HALT_ID].gaps, [[t_fail, t_back]])
        sent = h.step(OPEN)
        self.assertEqual([kinds(b) for b in sent], [["create", "transfer", "delete"]])
        p = sent[0][0]["payload"]
        self.assertEqual(p["gaps"], [[t_fail, t_back]])
        self.assertEqual((p["t0"], p["t1"]), (t0, t1))

    def test_failed_close_is_retried_on_the_next_poll(self):
        h = Harness()
        self.open_lease(h)
        h.step(OPEN)
        h.step(OPEN)
        h.w.fail_next = 1
        self.assertEqual(h.step(OPEN), [])
        self.assertEqual(h.w.attempts, 2)                   # lease + the failed close
        self.assertIn(HALT_ID, h.rep.opens)
        self.assertEqual([kinds(b) for b in h.step(OPEN)], [["create", "transfer", "delete"]])
        self.assertEqual(h.rep.opens, {})


# ---------------------------------------------------------------------------------------------
# Restart
# ---------------------------------------------------------------------------------------------

class RestartTests(EngineCase):
    def test_lapsed_lease_from_state_file_is_released_with_original_t0(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "state", "reporter.json")
            h = Harness(state_path=path)
            t0 = self.open_lease(h)
            old_key = h.rep.opens[HALT_ID].lease_key
            saved_at = h.clock.t
            self.assertTrue(os.path.exists(path))
            # the reporter is down for an hour: its 900-block lease lapses
            h.clock.t += 3600
            h.reg.head += 1800
            self.assertLessEqual(h.reg.entities[old_key]["expires"], h.reg.head)
            rep = h.new_reporter(state_path=path)
            rep.load()
            self.assertIn("kind = str('lease')", h.reg.queries[-1])
            self.assertIn(ME.lower(), h.reg.queries[-1])
            o = rep.opens[HALT_ID]
            self.assertEqual((o.lease_key, o.t0, o.gap_since), (old_key, t0, saved_at))
            # nothing is re-leased until the halt is seen again
            self.assertEqual(h.step(FAIL), [])
            sent = h.step(HALT)
            t_seen = h.clock.t
            self.assertEqual([kinds(b) for b in sent], [["create"]])
            c = sent[0][0]
            self.assertEqual((c["attrs"]["kind"], c["attrs"]["t0"]), ("lease", t0))
            self.assertEqual((c["payload"]["t0"], c["payload"]["resumed_from"]), (t0, old_key))
            new = rep.opens[HALT_ID]
            self.assertNotEqual(new.lease_key, old_key)
            self.assertIn(new.lease_key, h.reg.entities)
            self.assertEqual(new.gaps, [[saved_at, t_seen]])
            # and the closed record keeps the original start and the outage
            h.step(OPEN)
            h.step(OPEN)
            sent = h.step(OPEN)
            self.assertEqual([kinds(b) for b in sent], [["create", "transfer", "delete"]])
            p = sent[0][0]["payload"]
            self.assertEqual((p["t0"], p["lease_key"], p["gaps"]), (t0, new.lease_key, [[saved_at, t_seen]]))
            self.assertEqual(sent[0][2]["key"], new.lease_key)

    def test_live_lease_is_rebuilt_from_chain_without_a_state_file(self):
        h = Harness()
        t0 = self.open_lease(h)
        key = h.rep.opens[HALT_ID].lease_key
        rep = h.new_reporter(state_path=None)
        rep.load()
        o = rep.opens[HALT_ID]
        self.assertEqual((o.lease_key, o.t0, o.expires_at), (key, t0, h.reg.entities[key]["expires"]))
        self.assertEqual((o.net, o.listed, o.assets), ("xnet", 4, list(ASSETS)))
        self.assertEqual(o.resumed_from, "")
        self.assertEqual(h.step(HALT), [])                  # the lease is alive: no duplicate
        h.step(OPEN)
        h.step(OPEN)
        sent = h.step(OPEN)
        self.assertEqual([kinds(b) for b in sent], [["create", "transfer", "delete"]])
        self.assertEqual(sent[0][2]["key"], key)
        self.assertEqual(sent[0][0]["payload"]["t0"], t0)

    def test_rebuilt_lease_keeps_its_closed_count(self):
        # The lease payload stores the shut-asset count as "closed"; load() must read it back so an episode
        # closed right after a restart still reports how many assets were shut.
        h = Harness()
        self.open_lease(h)
        rep = h.new_reporter(state_path=None)
        rep.load()
        loaded = rep.opens[HALT_ID].max_closed
        h.step(OPEN)
        h.step(OPEN)
        sent = h.step(OPEN)
        self.assertEqual([kinds(b) for b in sent], [["create", "transfer", "delete"]])
        # (count held after load(), count written into the episode)
        self.assertEqual((loaded, sent[0][0]["payload"]["max_closed"]), (4, 4))


# ---------------------------------------------------------------------------------------------
# Pulses
# ---------------------------------------------------------------------------------------------

class PulseTests(EngineCase):
    def test_pulses_once_per_interval_with_four_creates(self):
        h = Harness(venues=vn.VENUES, pulses=True, pulse_every_s=3600, pulse_life=4500)
        self.assertEqual(len(vn.VENUES), 4)
        sent = h.step(OPEN)
        t_first = h.clock.t
        self.assertEqual([kinds(b) for b in sent], [["create"] * 4])
        self.assertEqual([c["attrs"]["venue"] for c in sent[0]], list(vn.VENUES))
        for c in sent[0]:
            v = c["attrs"]["venue"]
            self.assertEqual((c["attrs"]["app"], c["attrs"]["kind"]), ("doorledger", "pulse"))
            self.assertEqual((c["flags"], c["min_lifetime"]), (ak.READONLY, 4500))
            p = c["payload"]
            self.assertEqual((p["venue"], p["t"], p["polls_ok"], p["polls_fail"]), (v, t_first, 1, 0))
            self.assertEqual((p["items"], p["halted"], p["src"]), (100, 0, vn.URLS[v][0]))
            self.assertEqual(p["resp_sha256"], "resp-%d" % t_first)
        # within the hour: nothing
        self.assertEqual(h.step(OPEN, dt=1200), [])
        self.assertEqual(h.step(OPEN, dt=1200, kucoin=FAIL), [])
        # one interval after the first pulse: the next one, with the counts since
        sent = h.step(OPEN, dt=1200)
        self.assertEqual(h.clock.t - t_first, 3600)
        self.assertEqual([kinds(b) for b in sent], [["create"] * 4])
        counts = {c["attrs"]["venue"]: (c["payload"]["polls_ok"], c["payload"]["polls_fail"]) for c in sent[0]}
        self.assertEqual(counts, {"binance": (3, 0), "gate": (3, 0), "kucoin": (2, 1), "bithumb": (3, 0)})
        self.assertEqual(len(h.w.batches), 2)
        self.assertEqual(h.step(OPEN, dt=120), [])

    def test_failed_pulse_is_retried_next_cycle(self):
        h = Harness(venues=vn.VENUES, pulses=True, pulse_every_s=3600)
        h.w.fail_next = 1
        self.assertEqual(h.step(OPEN), [])
        self.assertEqual(h.w.attempts, 1)
        sent = h.step(OPEN)
        self.assertEqual([kinds(b) for b in sent], [["create"] * 4])
        self.assertEqual(sent[0][0]["payload"]["polls_ok"], 2)


# ---------------------------------------------------------------------------------------------
# Regression 1: a transaction whose outcome is unknown is settled before anything else is sent
# ---------------------------------------------------------------------------------------------

class PendingTxTests(EngineCase):
    def test_signed_tx_is_in_the_state_file_before_it_is_broadcast(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "reporter.json")
            h = Harness(state_path=path)
            at_broadcast = []

            def read_state(tx, nonce):
                with open(path, encoding="utf-8") as f:
                    at_broadcast.append((tx, nonce, json.load(f)["pending"]))
            h.w.on_broadcast = read_state
            self.open_lease(h)
            self.assertEqual(len(at_broadcast), 1)
            tx, nonce, p = at_broadcast[0]
            self.assertEqual((p["tx"], p["nonce"], p["kind"]), (tx, nonce, "open"))
            self.assertEqual(len(p["data"]["opens"]), 1)
            with open(path, encoding="utf-8") as f:
                self.assertIsNone(json.load(f)["pending"])     # cleared once the receipt was in

    def test_open_that_landed_unheard_is_not_leased_twice(self):
        h = Harness()
        self.assertEqual(h.step(HALT), [])
        h.w.outcomes = ["lost"]
        h.step(HALT)                       # the lease lands, but the reporter only gets ArkivPending
        self.assertEqual(len(h.created("lease")), 1)
        tx = h.w.signed[0][0]              # settled later in the same cycle, before any other write
        h.step(HALT)                       # still halted: the pending tx is settled by its receipt
        h.step(HALT)
        self.assertIsNone(h.rep.pending)
        self.assertEqual(len(h.created("lease")), 1, "a second lease was created for the same halt")
        self.assertEqual(len(h.live("lease")), 1)
        o = h.rep.opens[HALT_ID]
        self.assertEqual((o.lease_tx, o.lease_key), (tx, next(iter(h.live("lease")))))

    def test_close_that_landed_unheard_is_not_written_twice(self):
        h = Harness()
        self.open_lease(h)
        key = h.rep.opens[HALT_ID].lease_key
        h.step(OPEN)
        h.step(OPEN)
        h.w.outcomes = ["lost"]
        h.step(OPEN)                       # episode + burn + lease delete land; the reporter only gets ArkivPending
        self.assertEqual(len(h.created("episode")), 1)
        self.assertNotIn(key, h.reg.entities)
        h.step(OPEN)
        h.step(OPEN)
        self.assertIsNone(h.rep.pending)
        self.assertEqual(len(h.created("episode")), 1, "the same halt was published as a second episode")
        self.assertEqual(h.rep.opens, {})

    def test_open_mined_a_cycle_later_is_not_leased_twice(self):
        h = Harness()
        h.step(HALT)
        h.w.outcomes = ["stuck"]
        h.step(HALT)                       # the open is held unmined: the reporter must wait for it
        self.assertIsNotNone(h.rep.pending)
        h.step(HALT)                       # observe() sees the halt again before anything is settled
        h.w.mine_stuck()
        h.step(HALT)
        h.step(HALT)
        self.assertIsNone(h.rep.pending)
        self.assertEqual(len(h.created("lease")), 1, "the halt was leased twice")
        self.assertEqual(h.rep.opens[HALT_ID].lease_key, next(iter(h.live("lease"))))

    def test_close_mined_a_cycle_later_is_not_written_twice(self):
        h = Harness()
        self.open_lease(h)
        h.step(OPEN)
        h.step(OPEN)
        h.w.outcomes = ["stuck"]
        h.step(OPEN)                       # the close batch is held unmined
        self.assertIsNotNone(h.rep.pending)
        h.step(OPEN)                       # observe() lists the same close again before it is settled
        h.w.mine_stuck()
        h.step(OPEN)
        h.step(OPEN)
        self.assertIsNone(h.rep.pending)
        self.assertEqual(len(h.created("episode")), 1, "the same halt was published as a second episode")
        self.assertEqual(h.rep.opens, {})

    def test_pending_open_is_settled_after_a_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "reporter.json")
            h = Harness(state_path=path)
            h.step(HALT)
            h.w.outcomes = ["stuck"]
            h.step(HALT)
            tx = h.rep.pending["tx"]
            h.clock.t += 60                # the process dies and comes back from its state file
            h.reg.head += 30
            rep = h.new_reporter(state_path=path)
            rep.load()
            self.assertEqual(rep.pending["tx"], tx)
            h.w.mine_stuck()               # it lands while the new process is starting
            h.step(HALT)
            h.step(HALT)
            self.assertIsNone(rep.pending)
            self.assertEqual(len(h.created("lease")), 1)
            self.assertEqual(rep.opens[HALT_ID].lease_tx, tx)

    def test_pending_close_is_settled_after_a_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "reporter.json")
            h = Harness(state_path=path)
            self.open_lease(h)
            h.step(OPEN)
            h.step(OPEN)
            h.w.outcomes = ["stuck"]
            h.step(OPEN)
            self.assertEqual(len(h.created("episode")), 0)
            h.clock.t += 60
            h.reg.head += 30
            rep = h.new_reporter(state_path=path)
            rep.load()
            self.assertIsNotNone(rep.pending)
            h.w.mine_stuck()
            for _ in range(4):
                h.step(OPEN)
            self.assertIsNone(rep.pending)
            self.assertEqual(len(h.created("episode")), 1)
            self.assertEqual(rep.opens, {})

    def test_stuck_tx_holds_every_write_until_it_is_mined(self):
        h = Harness(venues=("gate", "binance"))
        h.step(HALT, binance=OPEN)
        h.step(HALT, binance=OPEN)
        key = h.rep.opens[HALT_ID].lease_key
        h.w.outcomes = ["stuck"]
        for _ in range(12):
            h.step(HALT, binance=OPEN)
            if h.rep.pending:
                break
        self.assertEqual(h.rep.pending["kind"], "renew")
        old_exp = h.rep.opens[HALT_ID].expires_at
        attempts = h.w.attempts
        # binance halts meanwhile and is confirmed, but nothing is sent while the renewal is unresolved
        h.step(HALT)
        h.step(HALT)
        self.assertEqual(h.w.attempts, attempts)
        self.assertNotIn(BIN_ID, h.rep.opens)
        self.assertEqual(h.rep.opens[HALT_ID].expires_at, old_exp)
        # mined: the next cycle applies the renewal, then sends the held lease
        h.w.mine_stuck()
        h.step(HALT)
        self.assertIsNone(h.rep.pending)
        self.assertGreater(h.rep.opens[HALT_ID].expires_at, old_exp)
        self.assertEqual(h.rep.opens[HALT_ID].expires_at, h.reg.entities[key]["expires"])
        self.assertIn(BIN_ID, h.rep.opens)
        self.assertEqual(len(h.created("lease")), 2)

    def test_dropped_tx_is_rebuilt_once_after_the_give_up_time(self):
        h = Harness()
        h.step(HALT)
        t_first = h.clock.t
        h.w.outcomes = ["dropped"]
        h.step(HALT)
        self.assertEqual(h.created("lease"), [])
        self.assertIsNotNone(h.rep.pending)
        attempts = h.w.attempts
        # the node no longer knows it but the nonce has not moved: hold until PENDING_GIVE_UP_S has passed
        h.step(HALT, dt=engine.PENDING_GIVE_UP_S // 2)
        self.assertEqual(h.w.attempts, attempts)
        self.assertIsNotNone(h.rep.pending)
        sent = h.step(HALT, dt=engine.PENDING_GIVE_UP_S // 2 + 60)
        self.assertIsNone(h.rep.pending)
        self.assertEqual([kinds(b) for b in sent], [["create"]])
        self.assertEqual(sent[0][0]["attrs"]["t0"], t_first)
        self.assertEqual(len(h.live("lease")), 1)
        self.assertEqual(h.rep.opens[HALT_ID].lease_key, next(iter(h.live("lease"))))

    def test_replaced_nonce_settles_as_dropped_at_once(self):
        h = Harness()
        h.step(HALT)
        h.w.outcomes = ["replaced"]
        h.step(HALT)
        sent = h.step(HALT)             # latest nonce is past it and there is no receipt: dropped, rebuilt now
        self.assertIsNone(h.rep.pending)
        self.assertEqual([kinds(b) for b in sent], [["create"]])
        self.assertEqual(len(h.created("lease")), 1)

    def test_pending_tx_that_reverted_on_chain_is_cleared_and_rebuilt(self):
        h = Harness()
        h.step(HALT)
        h.w.outcomes = ["stuck"]
        h.step(HALT)
        tx = h.rep.pending["tx"]
        h.w.mempool.clear()                # mined and reverted: no state change, nonce consumed
        h.w.nonce += 1
        h.w.reverted.add(tx)
        sent = h.step(HALT)
        self.assertIsNone(h.rep.pending)
        self.assertEqual([kinds(b) for b in sent], [["create"]])
        self.assertEqual(len(h.live("lease")), 1)
        self.assertTrue(any(tx in m and "reverted" in m for m in h.logs))

    def test_landed_tx_is_not_taken_for_dropped_when_its_receipt_lookup_fails(self):
        """ak.Writer.receipt answers None when eth_getTransactionReceipt itself errors. A landed transaction (its
        nonce has moved, and eth_getTransactionByHash shows it mined) must then not be written off as dropped,
        or its writes are sent a second time."""
        tx = "0x" + word(0xC105E)
        calls = []

        class StubRpc:
            def call(self, method, params):
                calls.append(method)
                if method == "eth_getTransactionReceipt":
                    raise ak.ArkivError("eth_getTransactionReceipt HTTP 503")
                if method == "eth_getTransactionCount":
                    return hex(8)
                if method == "eth_getTransactionByHash":
                    return {"hash": tx, "nonce": hex(7), "blockNumber": hex(START_HEAD)}
                raise AssertionError("unexpected %s" % method)
        w = ak.Writer(SimpleNamespace(address=ME), rpc=StubRpc(), log=lambda *_: None)
        rep = engine.Reporter(w, venues=("gate",), now=lambda: START_T + 30, log=lambda *_: None)
        rep.opens[HALT_ID] = engine.Open("gate", ROUTE, "xnet", "deposit", START_T - 3600, lease_key="0x" + word(1))
        rep.pending = {"tx": tx, "nonce": 7, "kind": "close", "data": {"ids": [list(HALT_ID)], "keys": []},
                       "sent_at": START_T}
        rep.resolve_pending()
        self.assertFalse(rep.pending is None and HALT_ID in rep.opens,
                         "the landed close was written off as dropped (calls: %s); it will be published again"
                         % calls)


# ---------------------------------------------------------------------------------------------
# Regression 2: a revert naming a lease that is already gone drops only that op
# ---------------------------------------------------------------------------------------------

class GoneLeaseTests(EngineCase):
    def two_leases(self) -> Harness:
        h = Harness(venues=("gate", "binance"))
        h.step(HALT)
        sent = h.step(HALT)
        self.assertEqual([kinds(b) for b in sent], [["create", "create"]])
        self.assertEqual({o.lease_key for o in h.rep.opens.values()}, set(h.live("lease")))
        return h

    def test_gone_lease_in_a_renew_batch_does_not_stop_the_other_renewal(self):
        for how in ("deleted", "expired"):
            with self.subTest(how=how):
                h = self.two_leases()
                g, b = h.rep.opens[HALT_ID], h.rep.opens[BIN_ID]
                gk, bk, t0 = g.lease_key, b.lease_key, b.t0
                self.assertEqual(g.expires_at, b.expires_at)
                for _ in range(12):
                    if b.expires_at - (h.reg.head + 60) < 900 * 2 // 3:
                        break                   # the next cycle renews both
                    h.step(HALT)
                if how == "deleted":
                    del h.reg.entities[bk]
                else:                           # the registry's expiry is earlier than the reporter thinks
                    h.reg.entities[bk]["expires"] = h.reg.head + 61
                attempts = h.w.attempts
                sent = h.step(HALT)
                self.assertEqual(h.w.attempts - attempts, 2)          # the full batch, then without the gone lease
                self.assertEqual([kinds(x) for x in sent], [["extend"]])
                self.assertEqual(sent[0][0]["key"], gk)
                self.assertEqual(h.rep.opens[HALT_ID].expires_at, h.reg.entities[gk]["expires"])
                self.assertEqual(h.rep.opens[BIN_ID].expires_at, 0)
                self.assertIsNone(h.rep.pending)
                self.assertTrue(any(bk in m and "already gone" in m for m in h.logs))
                # still seen: re-leased on the next cycle with its original t0, resuming the gone lease
                sent = h.step(HALT)
                self.assertEqual([kinds(x) for x in sent], [["create"]])
                c = sent[0][0]
                self.assertEqual((c["attrs"]["venue"], c["attrs"]["t0"], c["payload"]["resumed_from"]),
                                 ("binance", t0, bk))
                self.assertEqual(h.rep.opens[HALT_ID].lease_key, gk)

    def test_gone_lease_in_a_close_batch_still_writes_and_burns_the_episode(self):
        h = Harness()
        self.open_lease(h)
        key = h.rep.opens[HALT_ID].lease_key
        h.step(OPEN)
        h.step(OPEN)
        del h.reg.entities[key]
        sent = h.step(OPEN)
        self.assertEqual([kinds(b) for b in sent], [["create", "transfer"]])
        eps = h.live("episode")
        self.assertEqual(len(eps), 1)
        self.assertEqual(next(iter(eps.values()))["owner"], ak.BURN.lower())
        self.assertEqual(sent[0][0]["payload"]["lease_key"], key)
        self.assertEqual(h.rep.opens, {})

    def test_episode_is_never_published_without_its_burn(self):
        """The transfer to 0x...dEaD names the episode created in the same batch, never a lease. When that key
        is mispredicted (here: one entity-nonce read answered by a node a block behind) the batch must fail and
        be rebuilt, not go out without its burn."""
        h = Harness()
        self.open_lease(h)
        h.step(OPEN)
        h.step(OPEN)
        h.reg.stale_nonce_reads = 1
        for _ in range(3):
            h.step(OPEN)
        eps = {k: e for k, e in h.reg.entities.items() if e["attrs"]["kind"][1] == "episode"}
        self.assertTrue(eps)
        for k, e in eps.items():
            self.assertEqual(e["owner"], ak.BURN.lower(), "episode %s is still owned by its creator" % k)

    def test_stale_entity_nonce_does_not_leave_an_untracked_lease(self):
        """Same lagging read on the lease side: the reporter must end up tracking the lease the registry
        created, not create a second one for the same halt."""
        h = Harness(venues=("gate", "binance"))
        h.step(HALT, binance=OPEN)
        h.step(HALT, binance=OPEN)        # gate lease: this wallet's entity nonce is now 1
        h.step(HALT)
        h.reg.stale_nonce_reads = 1
        h.step(HALT)                       # binance lease, its key predicted from a stale nonce
        h.step(HALT)
        h.step(HALT)
        self.assertEqual(len([c for c in h.created("lease") if c["attrs"]["venue"] == "binance"]), 1,
                         "a second lease was created for the binance halt")
        self.assertLessEqual(set(h.live("lease")), {o.lease_key for o in h.rep.opens.values()})


# ---------------------------------------------------------------------------------------------
# Regression 3: a lease is renewed only while its halt is still seen
# ---------------------------------------------------------------------------------------------

class StaleLeaseTests(EngineCase):
    def test_lease_is_not_renewed_once_the_halt_is_no_longer_seen(self):
        h = Harness()
        t0 = self.open_lease(h)
        o = h.rep.opens[HALT_ID]
        key, seen = o.lease_key, o.seen_at
        renewed_after = []
        # XNET drops out of the response: the route can be neither confirmed halted nor judged open
        while h.reg.entities[key]["expires"] > h.reg.head:
            sent = h.step(GONE)
            for b in sent:
                self.assertEqual(kinds(b), ["extend"])
                renewed_after.append(h.clock.t - seen)
            self.assertLess(h.clock.t - START_T, 6 * 3600, "the lease never lapsed")
        self.assertTrue(renewed_after)                                # renewed while the sighting was recent
        self.assertTrue(all(s < engine.STALE_S for s in renewed_after), renewed_after)
        self.assertIn(HALT_ID, h.rep.opens)                           # never judged open, so never closed
        self.assertEqual(h.created("episode"), [])
        # seen again: a fresh lease with the original t0 that names the lapsed one
        sent = h.step(HALT)
        self.assertEqual([kinds(b) for b in sent], [["create"]])
        c = sent[0][0]
        self.assertEqual((c["attrs"]["t0"], c["payload"]["resumed_from"]), (t0, key))

    def test_original_assets_still_shut_keep_the_lease_alive_between_the_lines(self):
        h = Harness()
        self.open_lease(h)
        key = h.rep.opens[HALT_ID].lease_key
        diluted = {**{a: False for a in ASSETS}, "NEW1": True, "NEW2": True}   # 4/6 shut: between the lines
        renewals = 0
        for _ in range(30):                                            # an hour, twice STALE_S
            for b in h.step(diluted):
                self.assertEqual(kinds(b), ["extend"])
                renewals += 1
        self.assertGreaterEqual(renewals, 3)
        self.assertGreater(h.reg.entities[key]["expires"], h.reg.head)
        self.assertIn(HALT_ID, h.rep.opens)
        self.assertEqual(h.created("episode"), [])


# ---------------------------------------------------------------------------------------------
# Regression 4: hysteresis between the halt line and the open line
# ---------------------------------------------------------------------------------------------

SIX = ("AAA", "BBB", "CCC", "DDD", "KKK", "LLL")


class HysteresisTests(EngineCase):
    def open_six(self, h: Harness) -> None:
        shut = {a: False for a in SIX}
        self.assertEqual(h.step(shut), [])
        self.assertEqual([kinds(b) for b in h.step(shut)], [["create"]])
        self.assertEqual(h.rep.opens[HALT_ID].assets, sorted(SIX))

    def test_partial_reopen_between_the_lines_does_not_close(self):
        h = Harness()
        self.open_six(h)
        partial = {a: a in ("KKK", "LLL") for a in SIX}               # 4/6 still shut
        for _ in range(6):
            for b in h.step(partial):
                self.assertNotIn("create", kinds(b))
        self.assertIn(HALT_ID, h.rep.opens)
        self.assertNotIn(HALT_ID, h.rep.open_streak)
        self.assertEqual(h.created("episode"), [])
        # at or below the open line (2/6 shut): three polls close it
        low = {a: a not in ("AAA", "BBB") for a in SIX}
        h.step(low)
        t1 = h.clock.t
        h.step(low)
        sent = h.step(low)
        self.assertEqual([kinds(b) for b in sent], [["create", "transfer", "delete"]])
        self.assertEqual(sent[0][0]["payload"]["t1"], t1)

    def test_a_poll_between_the_lines_restarts_the_close_count(self):
        h = Harness()
        self.open_six(h)
        clean = {a: True for a in SIX}
        partial = {a: a in ("KKK", "LLL") for a in SIX}
        for state in (clean, clean, partial, clean, clean):
            h.step(state)
        self.assertEqual(h.created("episode"), [])
        sent = h.step(clean)
        self.assertEqual([kinds(b) for b in sent], [["create", "transfer", "delete"]])
        self.assertEqual(sent[0][0]["payload"]["t1"], h.clock.t - 240)  # first poll of the unbroken run

    def test_halt_closes_when_its_own_assets_reopen_although_others_are_shut(self):
        h = Harness()
        self.open_lease(h)                                             # AAA..DDD shut
        moved = {**{a: True for a in ASSETS}, "NEW1": False, "NEW2": False, "NEW3": False}  # 3/7 shut
        self.assertGreater(3, vn.OPEN_LINE * 7)
        h.step(moved)
        h.step(moved)
        sent = h.step(moved)
        self.assertEqual([kinds(b) for b in sent], [["create", "transfer", "delete"]])
        self.assertEqual(h.rep.opens, {})


# ---------------------------------------------------------------------------------------------
# Regression 5: restart keeps what the reporter knew
# ---------------------------------------------------------------------------------------------

class RestartStateTests(EngineCase):
    def test_restart_with_a_live_lease_keeps_its_coverage_record(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "reporter.json")
            h = Harness(state_path=path)
            t0 = self.open_lease(h)
            h.step(FAIL)
            t_fail = h.clock.t
            h.step(HALT)
            t_back = h.clock.t
            o = h.rep.opens[HALT_ID]
            before = (o.lease_key, o.lease_tx, o.polls, [list(g) for g in o.gaps], o.first_sha256, o.seen_at)
            self.assertEqual((o.polls, o.gaps), (3, [[t_fail, t_back]]))
            saved_at = h.clock.t
            h.clock.t += 600                                           # down 10 minutes; the lease is still live
            h.reg.head += 300
            self.assertGreater(h.reg.entities[o.lease_key]["expires"], h.reg.head)
            n_calls = len(h.reg.calls)
            rep = h.new_reporter(state_path=path)
            rep.load()
            r = rep.opens[HALT_ID]
            self.assertEqual((r.lease_key, r.lease_tx, r.polls, r.gaps, r.first_sha256, r.seen_at), before)
            self.assertEqual((r.t0, r.gap_since), (t0, saved_at))
            self.assertEqual(h.reg.calls[n_calls:], [])                # lease_tx came from the state file
            sent = h.step(HALT)
            t_up = h.clock.t
            self.assertNotIn("create", [k for b in sent for k in kinds(b)])
            self.assertEqual(len(h.created("lease")), 1)
            self.assertEqual(r.gaps, [[t_fail, t_back], [saved_at, t_up]])
            h.step(OPEN)
            h.step(OPEN)
            sent = h.step(OPEN)
            self.assertEqual([kinds(b) for b in sent], [["create", "transfer", "delete"]])
            p = sent[0][0]["payload"]
            self.assertEqual((p["lease_tx"], p["polls"], p["gaps"], p["first_sha256"], p["t0"]),
                             (before[1], 4, [[t_fail, t_back], [saved_at, t_up]], before[4], t0))

    def test_restart_during_an_outage_keeps_the_gap_start(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "reporter.json")
            h = Harness(state_path=path)
            self.open_lease(h)
            h.step(FAIL)
            t_lost = h.clock.t
            h.step(FAIL)                                               # still unreadable when it stops
            self.assertEqual(h.rep.opens[HALT_ID].gap_since, t_lost)
            h.clock.t += 300
            h.reg.head += 150
            rep = h.new_reporter(state_path=path)
            rep.load()
            h.step(HALT)
            self.assertEqual(rep.opens[HALT_ID].gaps, [[t_lost, h.clock.t]],
                             "the coverage gap must start when the venue became unreadable")

    def test_lease_tx_is_recovered_from_the_creation_log(self):
        h = Harness()
        self.open_lease(h)
        o = h.rep.opens[HALT_ID]
        rep = h.new_reporter(state_path=None)
        rep.load()
        self.assertEqual(rep.opens[HALT_ID].lease_tx, o.lease_tx)
        method, params = h.reg.calls[-1]
        self.assertEqual(method, "eth_getLogs")
        f = params[0]
        self.assertEqual((f["address"].lower(), f["topics"]), (REGISTRY, [ak.TOPIC_CREATED, o.lease_key]))
        self.assertEqual(f["fromBlock"], f["toBlock"])
        h.step(OPEN)
        h.step(OPEN)
        sent = h.step(OPEN)
        self.assertEqual(sent[0][0]["payload"]["lease_tx"], o.lease_tx)

    def test_failed_log_lookup_still_loads_the_lease(self):
        h = Harness()
        self.open_lease(h)
        key = h.rep.opens[HALT_ID].lease_key
        h.reg.fail_methods.add("eth_getLogs")
        rep = h.new_reporter(state_path=None)
        rep.load()
        self.assertEqual((rep.opens[HALT_ID].lease_key, rep.opens[HALT_ID].lease_tx), (key, ""))

    def test_state_file_with_unknown_or_missing_fields_still_loads(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "reporter.json")
            h = Harness(state_path=path)
            self.open_lease(h)
            o = h.rep.opens[HALT_ID]
            with open(path, encoding="utf-8") as f:
                st = json.load(f)
            st["opens"][0]["field_from_a_newer_version"] = [1, 2]
            del st["opens"][0]["seen_at"]                              # a field an older version did not write
            st["another_new_field"] = {"x": 1}
            with open(path, "w", encoding="utf-8") as f:
                json.dump(st, f)
            rep = h.new_reporter(state_path=path)
            rep.load()
            r = rep.opens[HALT_ID]
            self.assertEqual((r.lease_key, r.lease_tx, r.polls), (o.lease_key, o.lease_tx, o.polls))

    def test_only_this_reporters_leases_and_pulses_are_read(self):
        h = Harness(venues=vn.VENUES, pulses=True)
        h.step(HALT)
        h.step(HALT)
        q = h.reg.queries
        rep = h.new_reporter(state_path=None)
        rep.load()
        self.assertIn("kind = str('pulse')", q[-1])
        self.assertIn("$owner = addr(%s)" % ME.lower(), q[-1])
        self.assertEqual(rep.last_pulse_block, h.w.receipts[h.w.signed[0][0]].block)
        # another wallet's lease for the same halt is not taken over
        other = FakeWriter(h.reg, addr=to_checksum_address("0x" + "ab" * 20))
        other_rep = engine.Reporter(other, venues=("gate",), poll=h.poll, now=h.clock, log=h.logs.append,
                                    pulse_every_s=NO_PULSES)
        other_rep.load()
        self.assertEqual(other_rep.opens, {})


# ---------------------------------------------------------------------------------------------
# Regression 7: the short-response guard looks back 24 hours, not forever
# ---------------------------------------------------------------------------------------------

class ShortResponseWindowTests(EngineCase):
    def test_gradual_decline_over_more_than_a_day_does_not_blind_the_venue(self):
        h = Harness()
        hours = 48
        for i in range(hours + 1):
            h.items = round(1000 - 120 * i / hours)                    # 1000 -> 880 items (-12 %)
            h.extra_tradable = round(100 - 60 * i / hours)             # 107 -> 47 tradable coins (-56 %)
            h.step(OPEN, dt=3600, blocks=1800)
            self.assertEqual(h.rep.tick["gate"]["fail"], 0, "hour %d: %s" % (i, h.logs[-1:]))
        self.assertLessEqual(len(h.rep.size_hist["gate"]), 25)
        # the venue is still watched: a halt opens normally
        h.step(HALT)
        self.assertEqual([kinds(b) for b in h.step(HALT)], [["create"]])
        # and the guard still trips on a sudden drop inside the window
        h.items = int(880 * 0.85)
        h.step(OPEN)
        self.assertEqual(h.rep.tick["gate"]["fail"], 1)
        h.items, h.extra_tradable = 880, 10
        h.step(OPEN)
        self.assertEqual(h.rep.tick["gate"]["fail"], 2)
        self.assertIn("short tradable list", h.logs[-1])

    def test_size_history_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "reporter.json")
            h = Harness(state_path=path)
            h.items = 1000
            h.step(OPEN)
            rep = h.new_reporter(state_path=path)
            rep.load()
            h.items = 800
            h.step(OPEN)
            self.assertEqual(rep.tick["gate"]["fail"], 1)


# ---------------------------------------------------------------------------------------------
# Smaller fixes: sampled coverage, payload names, pulse spacing, batched keys
# ---------------------------------------------------------------------------------------------

class SampledModeTests(EngineCase):
    def test_once_mode_episode_makes_no_coverage_claims(self):
        h = Harness(sampled=True, lease_life=7200)
        self.open_lease(h)
        c = h.created("lease")[0]
        self.assertEqual((c["payload"]["coverage"], c["min_lifetime"]), ("sampled", 7200))
        h.step(FAIL)
        self.assertEqual(h.rep.opens[HALT_ID].gap_since, 0)
        h.step(OPEN)
        h.step(OPEN)
        sent = h.step(OPEN)
        self.assertEqual([kinds(b) for b in sent], [["create", "transfer", "delete"]])
        p = sent[0][0]["payload"]
        self.assertEqual(p["coverage"], "sampled")
        self.assertNotIn("polls", p)
        self.assertNotIn("gaps", p)

    def test_once_mode_restart_records_no_downtime_gap(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "reporter.json")
            h = Harness(sampled=True, lease_life=7200, state_path=path)
            self.open_lease(h)
            h.clock.t += 3600
            h.reg.head += 1800
            rep = h.new_reporter(state_path=path)
            rep.load()
            self.assertEqual(rep.opens[HALT_ID].gap_since, 0)
            h.step(HALT)
            self.assertEqual(rep.opens[HALT_ID].gaps, [])

    def run_once(self, h: Harness) -> engine.Reporter:
        """What `python -m writer.run --once` does, against the fakes."""
        rep = h.new_reporter(state_path=None, sampled=True, lease_life=7200, pulse_every_s=0, pulse_life=5400)
        rep.load()
        for _ in range(3):
            h.clock.t += 60
            h.reg.head += 30
            to_open, to_close = rep.observe()
            if rep.resolve_pending():
                rep.write_closes(to_close)
                rep.write_opens(to_open)
        if rep.resolve_pending():
            rep.write_heartbeat()
            rep.write_pulses(force=True)
        return rep

    def test_once_mode_pulses_at_most_every_pulse_gap_blocks(self):
        h = Harness(venues=vn.VENUES)
        self.run_once(h)
        self.assertEqual(len(h.created("pulse")), 4)
        first = h.reg.head
        h.clock.t += 1800                                              # a second run 30 minutes later
        h.reg.head += 900
        self.run_once(h)
        self.assertEqual(len(h.created("pulse")), 4)                   # it read the earlier pulses back
        h.reg.head = first + engine.PULSE_GAP_BLOCKS
        h.clock.t += 1800
        self.run_once(h)
        self.assertEqual(len(h.created("pulse")), 8)


class PayloadAndKeyTests(EngineCase):
    def test_recovery_estimate_is_published_as_eta_unix_ms(self):
        eta = 1_800_100_000_000
        h = Harness()

        def poll(venue):
            s = h.poll(venue)
            for r in s.rows:
                if r.route == ROUTE:
                    r.eta_ms = eta
            return s
        h.rep.poll = poll
        self.open_lease(h)
        p = h.created("lease")[0]["payload"]
        self.assertEqual(p["eta_unix_ms"], eta)
        self.assertNotIn("eta_ms", p)
        rep = h.new_reporter(state_path=None)
        rep.load()
        self.assertEqual(rep.opens[HALT_ID].eta_unix_ms, eta)
        h.step(OPEN)
        h.step(OPEN)
        sent = h.step(OPEN)
        self.assertEqual(sent[0][0]["payload"]["eta_unix_ms"], eta)
        self.assertNotIn("eta_ms", sent[0][0]["payload"])

    def test_two_closes_in_one_batch_burn_both_episodes(self):
        h = Harness(venues=("gate", "binance"))
        h.step(HALT)
        h.step(HALT)
        self.assertEqual(len(h.live("lease")), 2)
        h.step(OPEN)
        h.step(OPEN)
        sent = h.step(OPEN)
        self.assertEqual([kinds(b) for b in sent], [["create", "transfer", "delete"] * 2])
        eps = h.live("episode")
        self.assertEqual(len(eps), 2)
        self.assertTrue(all(e["owner"] == ak.BURN.lower() for e in eps.values()))
        self.assertEqual({sent[0][1]["key"], sent[0][4]["key"]}, set(eps))
        self.assertEqual(h.live("lease"), {})
        self.assertEqual(h.rep.opens, {})


if __name__ == "__main__":
    unittest.main()
