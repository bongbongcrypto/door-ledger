"""Re-run every claim in arkiv/friction.md against Tiramisu and save what the node answered.

    python tools/friction_probe.py              # all items; writes arkiv/evidence/friction-<utc date>.json
    python tools/friction_probe.py --only untagged_number,cursor_without_atblock   # some items, no file

Read-only by construction:
  * plain JSON-RPC reads (arkiv_query, arkiv_getEntity, arkiv_getEntityCount, eth_getLogs, ...);
  * writes are only *simulated* with eth_estimateGas / eth_call from 0x...beef (no signature needed);
  * nothing is signed or sent (Probe.rpc refuses send/sign methods) and no key file is opened.
Budget: about 15 arkiv_query calls and under 60 requests in total. On an HTTP 429 the remaining query
items are skipped and the evidence file is still written.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import importlib.metadata as md
import json
import os
import platform
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from eth_abi import decode as abi_decode  # noqa: E402
from eth_abi import encode as abi_encode  # noqa: E402
from eth_utils import keccak, to_checksum_address  # noqa: E402

from writer import arkiv as ak  # noqa: E402

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
WSS_URL = os.environ.get("ARKIV_WSS", "wss://rpc.tiramisu.db-chain.testnet.arkiv.network")
BEEF = "0x000000000000000000000000000000000000beef"   # simulation sender; estimateGas needs no key
DEAD = "0x000000000000000000000000000000000000dead"
ORIGIN = "https://example.github.io"                 # a static-site origin, as a browser would send it
DOCS = "Arkiv-Network/arkiv-starlight-docs@45099f7 (develop; main d64a177 has the same content in the cited files)"
UA = "door-ledger-friction-probe/1"
REFUSED = ("eth_sendRawTransaction", "eth_sendTransaction", "eth_sign", "eth_signTransaction", "personal_")

# The "Full calldata for this example" block in src/content/docs/json-rpc/mutating-entities.mdx (45099f7).
DOCS_EXAMPLE_CALLDATA = (
    "0x496500440000000000000000000000000000000000000000000000000000000000000020000000000000000000000000000000"
    "00000000000000000000000000000000010000000000000000000000000000000000000000000000000000000000000020000000"
    "00000000000000000000000000000000000000000000000000000000010000000000000000000000000000000000000000000000"
    "00000000000000004000000000000000000000000000000000000000000000000000000000000003e00000000000000000000000"
    "00000000000000000000000000000000000000002000000000000000000000000000000000000000000000000000000000000012"
    "34000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000013c680000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000000000000a0000000000000000000000000000000000000000000000000000000"
    "00000000040000000000000000000000000000000000000000000000000000000000000080000000000000000000000000000000"
    "000000000000000000000000000000012000000000000000000000000000000000000000000000000000000000000001c0000000"
    "000000000000000000000000000000000000000000000000000000026024636f6e74656e74547970650000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000080000000000000000000000"
    "00000000000000000000000000000000000000006000000000000000000000000000000000000000000000000000000000000000"
    "106170706c69636174696f6e2f6a736f6e00000000000000000000000000000000247061796c6f61640000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000700000000000000"
    "00000000000000000000000000000000000000000000000060000000000000000000000000000000000000000000000000000000"
    "000000001e7b226d657373616765223a2248656c6c6f2066726f6d2041726b6976227d000063617465676f727900000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000008000000"
    "00000000000000000000000000000000000000000000000000000000600000000000000000000000000000000000000000000000"
    "0000000000000000076578616d706c6500000000000000000000000000000000000000000000000000636f756e74000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "03000000000000000000000000000000000000000000000000000000000000006000000000000000000000000000000000000000"
    "000000000000000000000000200000000000000000000000000000000000000000000000000000000000000064"
)

EVENTS = {"0x" + keccak(s.encode()).hex(): s.split("(")[0] for s in (
    "EntityCreated(bytes32,address,uint64,uint8)", "EntityPatched(bytes32,address)",
    "ExpiryExtended(bytes32,address,uint64)", "OwnershipTransferred(bytes32,address,address)",
    "EntityDeleted(bytes32,address)")}
ERRORS = {"0x" + keccak(s.encode()).hex()[:8]: s for s in (
    "Ident32InvalidByte(uint256,bytes1)", "Ident32Empty()", "ReadOnlyEntity(bytes32)", "EntityExpired(bytes32,uint64)",
    "EntityNotFound(bytes32)", "NotOwner(bytes32,address,address)", "ExpiryNotExtended(bytes32,uint64,uint64)",
    "ExpiryDeadOnArrival(uint64,uint64)", "InvalidValueType(bytes32,uint8)", "SystemAttributeNotWritable(bytes32)",
    "AttributesNotSorted()", "ReservedCreationFlags(uint8)", "TooManyAttributes(uint256,uint256)")}
KEEP_HEADERS = ("arkiv-", "ratelimit", "retry-after", "x-ratelimit", "access-control-")


class Throttled(Exception):
    pass


def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def short(v, n: int = 600):
    s = json.dumps(v, separators=(",", ":"))
    return v if len(s) <= n else s[:n] + "...(%d chars)" % len(s)


def decode_revert(data) -> str:
    if not isinstance(data, str) or len(data) < 10:
        return "no revert data"
    sig = ERRORS.get(data[:10].lower())
    if not sig:
        return "unknown revert %s" % data[:10]
    types = [t for t in sig[sig.index("(") + 1:-1].split(",") if t]
    vals = abi_decode(types, bytes.fromhex(data[10:])) if types else ()
    out = []
    for t, v in zip(types, vals):
        out.append(cut("0x" + v.hex()) if isinstance(v, bytes) else str(v).lower() if t == "address" else str(v))
    return "%s(%s)" % (sig.split("(")[0], ", ".join(out))


def cut(h: str) -> str:
    """0x1234...abcd for 32-byte values in printed summaries (the raw call records keep full values)."""
    return h[:6] + "..." + h[-4:] if isinstance(h, str) and len(h) == 66 else h


def compact(method: str, res):
    """Keep the fields that carry evidence; drop block hashes, signatures and other bulk."""
    if method == "eth_getLogs" and isinstance(res, list):
        return [{"event": EVENTS.get(lg["topics"][0], "?"), "key": lg["topics"][1] if len(lg["topics"]) > 1 else None,
                 "block": int(lg["blockNumber"], 16)} for lg in res]
    if method == "eth_getTransactionByHash" and isinstance(res, dict):
        return {"from": res.get("from"), "to": res.get("to"), "blockNumber": res.get("blockNumber"),
                "input_bytes": (len(res.get("input", "")) - 2) // 2, "input_head": res.get("input", "")[:10]}
    if method == "eth_getTransactionReceipt" and isinstance(res, dict):
        return {"status": res.get("status"), "blockNumber": res.get("blockNumber"), "gasUsed": res.get("gasUsed"),
                "from": res.get("from"),
                "logs": [{"event": EVENTS.get(lg["topics"][0], "?"), "key": lg["topics"][1],
                          "topic2_address": topic_addr(lg["topics"][2]) if len(lg["topics"]) > 2 else None,
                          "data_words": [int(lg["data"][2 + i:66 + i], 16) for i in range(0, len(lg["data"]) - 2, 64)]}
                         for lg in res.get("logs", [])]}
    if method == "eth_call" and isinstance(res, str) and len(res) == 66:
        return int(res, 16)
    if method == "arkiv_getEntity" and isinstance(res, dict):
        out = {k: res.get(k) for k in ("key", "owner", "creator", "createdAt", "expiresAt", "creationFlags")}
        out["payload_bytes"] = (len(res.get("payload") or "0x") - 2) // 2
        return out
    if method == "arkiv_query" and isinstance(res, dict):
        out = {"blockNumber": res.get("blockNumber"), "rows": len(res.get("data") or [])}
        if res.get("cursor"):
            out["cursor"] = res["cursor"]
        if res.get("data"):
            out["first_row"] = short(res["data"][0], 400)
        return out
    return short(res)


def raw_attr(name: str, value: bytes = b"x") -> tuple:
    """A str attribute with any name, bypassing writer/arkiv.py's own name check on purpose."""
    return (name.encode("ascii").ljust(32, b"\0"), ak.T_STR, value)


def op_patch(key: str, attrs: list[tuple]) -> ak.Op:
    return ak.Op(ak.OP_PATCH, abi_encode(["(bytes32,(bytes32,uint8,bytes)[])"], [(bytes.fromhex(key[2:]), attrs)]),
                 "patch", key)


def topic_addr(t: str) -> str:
    return "0x" + t[-40:].lower()


class Probe:
    def __init__(self, url: str):
        self.url = url
        self.calls = 0
        self.queries = 0
        self.throttled = None
        self.records: list[dict] = []      # every call, for the rate-limit item
        self.head = 0

    # -- transport ------------------------------------------------------------------------------
    def _http(self, req):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def rpc(self, method: str, params: list, rec: list | None = None):
        if method.startswith(REFUSED):
            raise RuntimeError("refusing %s: this probe never signs or sends" % method)
        if method == "arkiv_query":
            if self.throttled:
                raise Throttled(self.throttled)
            self.queries += 1
        self.calls += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self.calls, "method": method, "params": params}).encode()
        req = urllib.request.Request(self.url, body, {"Content-Type": "application/json",
                                                      "User-Agent": UA})
        t = utc()
        status, headers, raw = self._http(req)
        hdr = {k.lower(): v for k, v in headers.items() if k.lower().startswith(KEEP_HEADERS)}
        try:
            out = json.loads(raw)
        except ValueError:
            out = {"raw": raw[:300].decode("utf-8", "replace")}
        r = {"utc": t, "method": method, "params": params, "http": status, "headers": hdr}
        res = err = None
        if isinstance(out, dict) and "result" in out:
            res = out["result"]
            r["result"] = compact(method, res)
        elif isinstance(out, dict) and "error" in out and isinstance(out["error"], dict):
            err = out["error"]
            r["error"] = err
            if isinstance(err.get("data"), str) and err["data"].startswith("0x"):
                r["revert"] = decode_revert(err["data"])
        else:
            err = {"http": status, "body": out}
            r["body"] = out
        if status == 429:
            self.throttled = "HTTP 429 at %s: %s" % (t, short(out, 300))
        if method == "arkiv_query" and isinstance(res, dict) and res.get("blockNumber"):
            self.head = max(self.head, int(res["blockNumber"], 16))
        self.records.append(r)
        if rec is not None:
            rec.append(r)
        if status == 429 and method == "arkiv_query":
            raise Throttled(self.throttled)
        return res, err

    # -- helpers ------------------------------------------------------------------------------
    def block(self, rec) -> int:
        res, _ = self.rpc("eth_blockNumber", [], rec)
        self.head = int(res, 16)
        return self.head

    def query(self, q: str, opts: dict | None, rec):
        return self.rpc("arkiv_query", [q, opts], rec)

    def simulate(self, ops: list[ak.Op], rec, sender: str = BEEF):
        """('ok', gas) or ('revert', decoded error) from eth_estimateGas. Nothing is signed."""
        res, err = self.rpc("eth_estimateGas", [{"from": sender, "to": ak.REGISTRY,
                                                 "data": ak.execute_calldata(ops)}], rec)
        if err is None:
            return "ok", int(res, 16)
        return "revert", decode_revert(err.get("data")) if isinstance(err, dict) else str(err)

    def entity_nonce(self, owner: str, rec, block="latest") -> int:
        data = "0x" + (ak.SEL_ENTITY_NONCE + abi_encode(["address"], [to_checksum_address(owner)])).hex()
        res, err = self.rpc("eth_call", [{"to": ak.REGISTRY, "data": data}, block], rec)
        if err:
            raise RuntimeError("entityNonce failed: %s" % err)
        return int(res, 16)


def rows(res) -> int:
    return len(res.get("data") or []) if isinstance(res, dict) else -1


def code(err) -> str:
    return "none" if not err else str(err.get("code", err.get("http")))


def burn_check() -> dict | None:
    files = sorted(glob.glob(os.path.join(ROOT, "arkiv", "evidence", "burn-check-*.json")))
    if not files:
        return None
    with open(files[-1], encoding="utf-8") as f:
        d = json.load(f)
    steps = {s["step"]: s for s in d.get("steps", [])}
    try:
        return {"file": "arkiv/evidence/" + os.path.basename(files[-1]), "creator": d["creator"].lower(),
                "third_party": d["third_party"].lower(), "key": steps["create+transfer"]["created"][0].lower(),
                "create_tx": steps["create+transfer"]["tx"], "extend_tx": steps["third party extends"]["tx"]}
    except (KeyError, IndexError):
        return None


# =============================================================================================
# Items. Each returns (status, summary); calls go into `rec`. ctx carries values between items.
# =============================================================================================

def untagged_number(p: Probe, rec, ctx):
    sel = {"key": True, "attributes": True}
    res, err = p.query("app = str('doorledger') AND t0 >= u64(0)", {"limit": 1, "select": sel}, rec)
    if rows(res) > 0:
        base, attr = "app = str('doorledger')", "t0"
        tagged = res
    else:   # no Door Ledger rows yet: borrow any live entity that carries a u64 attribute
        res, err = p.query("*", {"limit": 20, "select": sel}, rec)
        found = [(r["key"], a["name"]) for r in (res or {}).get("data", []) for a in r.get("attributes", [])
                 if a.get("type") == "u64"]
        if not found:
            return "SKIPPED", "no live entity with a u64 attribute found"
        k, attr = found[0]
        base = "$key = key(%s)" % k
        tagged, err = p.query("%s AND %s >= u64(0)" % (base, attr), {"limit": 1}, rec)
    at = tagged["blockNumber"]
    ctx.update(base=base, attr=attr, at=at, tagged_rows=rows(tagged))
    un, un_err = p.query("%s AND %s >= 0" % (base, attr), {"limit": 1, "atBlock": at}, rec)
    _, str_err = p.query("app >= str('a')", {"limit": 1, "atBlock": at}, rec)
    _, sys_err = p.query("$createdAt >= 0", {"limit": 1, "atBlock": at}, rec)
    ok = rows(tagged) >= 1 and rows(un) == 0 and un_err is None and code(str_err) == "-32002" \
        and code(sys_err) == "-32002"
    return ("OBSERVED" if ok else "NOT REPRODUCED",
            "%s >= u64(0): %d row(s); %s >= 0: %d row(s), error %s; app >= str('a'): error %s; "
            "$createdAt >= 0: error %s (%s)" % (attr, rows(tagged), attr, rows(un), code(un_err), code(str_err),
                                                 code(sys_err), (sys_err or {}).get("message", "")))


def uppercase_name(p: Probe, rec, ctx):
    lower = p.simulate([ak.op_create([raw_attr("kind")], min_lifetime=100, salt=0xA1)[0]], rec)
    upper = p.simulate([ak.op_create([raw_attr("Kind")], min_lifetime=100, salt=0xA1)[0]], rec)
    if "base" not in ctx:
        return "SKIPPED", "write half only: kind -> %s %s, Kind -> %s %s" % (lower + upper)
    cap = ctx["attr"][0].upper() + ctx["attr"][1:]
    res, err = p.query("%s AND %s >= u64(0)" % (ctx["base"], cap), {"limit": 1, "atBlock": ctx["at"]}, rec)
    ok = lower[0] == "ok" and upper[0] == "revert" and "Ident32InvalidByte" in upper[1] and rows(res) == 0 \
        and err is None and ctx["tagged_rows"] >= 1
    return ("OBSERVED" if ok else "NOT REPRODUCED",
            "write 'kind': %s %s; write 'Kind': %s %s; query %s (vs %s, %d row(s)): %d row(s), error %s"
            % (lower[0], lower[1], upper[0], upper[1], cap, ctx["attr"], ctx["tagged_rows"], rows(res), code(err)))


def reserved_word_name(p: Probe, rec, ctx):
    made = p.simulate([ak.op_create([raw_attr("and"), raw_attr("key"), raw_attr("str")], min_lifetime=100,
                                    salt=0xA2)[0]], rec)
    _, e_and = p.query("and = str('x')", {"limit": 1}, rec)
    _, e_str = p.query("str = str('x')", {"limit": 1}, rec)
    ok = made[0] == "ok" and e_and is not None and e_str is not None
    return ("OBSERVED" if ok else "NOT REPRODUCED",
            "create with attributes and/key/str: %s %s; query and = ...: %s %s; query str = ...: %s %s"
            % (made[0], made[1], code(e_and), (e_and or {}).get("message", ""), code(e_str),
               (e_str or {}).get("message", "")))


def cursor_without_atblock(p: Probe, rec, ctx):
    page1, err = p.query("*", {"limit": "0x1"}, rec)
    if err or not page1.get("cursor"):
        return "SKIPPED", "page 1 gave no cursor (%s)" % code(err)
    b1 = int(page1["blockNumber"], 16)
    for _ in range(6):                      # let the head move past page 1's block
        time.sleep(2.5)
        if p.block(rec) > b1:
            break
    page2, err2 = p.query("*", {"cursor": page1["cursor"], "limit": "0x1"}, rec)
    page2b, err3 = p.query("*", {"cursor": page1["cursor"], "limit": "0x1", "atBlock": page1["blockNumber"]}, rec)
    ok = p.head > b1 and code(err2) == "-32005" and err3 is None and rows(page2b) == 1
    return ("OBSERVED" if ok else "NOT REPRODUCED",
            "page 1 at block %d, cursor %s...; head now %d; page 2 as in the docs (cursor only): %s %s; "
            "page 2 with atBlock=%s: %s, %d row(s)"
            % (b1, page1["cursor"][:6], p.head, code(err2), (err2 or {}).get("message", ""), page1["blockNumber"],
               code(err3), rows(page2b)))


def block_param_encoding(p: Probe, rec, ctx):
    bc = ctx.get("burn")
    if not bc:
        return "SKIPPED", "no burn-check evidence file to take a live key from"
    b = p.block(rec)
    q = "$owner = addr(%s)" % DEAD
    n_num, e_num = p.rpc("arkiv_getEntityCount", [{"query": q, "block": b}], rec)
    n_hex, e_hex = p.rpc("arkiv_getEntityCount", [{"query": q, "block": hex(b)}], rec)
    g_num, eg_num = p.rpc("arkiv_getEntity", [bc["key"], b], rec)
    g_hex, eg_hex = p.rpc("arkiv_getEntity", [bc["key"], hex(b)], rec)
    g_sel, eg_sel = p.rpc("arkiv_getEntity", [bc["key"], {"select": {"owner": True}}], rec)
    q_num, eq_num = p.query("$key = key(%s)" % bc["key"], {"atBlock": b, "limit": 1}, rec)

    def show(res, err):
        return ("error %s: %s" % (code(err), err.get("message", ""))) if err else "ok"
    ok = e_num is None and e_hex is not None
    return ("OBSERVED" if ok else "NOT REPRODUCED",
            "getEntityCount block=%d: %s (%s); block=\"%s\": %s | getEntity block=%d: %s; block=\"%s\": %s; "
            "{select}: %s | arkiv_query atBlock=%d (number): %s"
            % (b, show(n_num, e_num), n_num, hex(b), show(n_hex, e_hex), b, show(g_num, eg_num), hex(b),
               show(g_hex, eg_hex), show(g_sel, eg_sel), b, show(q_num, eq_num)))


def updated_at_queryable(p: Probe, rec, ctx):
    res, err = p.query("$updatedAt >= u64(0)", {"limit": 1}, rec)
    return ("OBSERVED" if err else "NOT REPRODUCED",
            "$updatedAt >= u64(0): %s" % (("error %s: %s" % (code(err), err.get("message", ""))) if err
                                          else "%d row(s)" % rows(res)))


def unix_seconds_expiry(p: Probe, rec, ctx):
    b = p.block(rec)
    ts = int(time.time()) + 86_400          # "tomorrow" written as a unix timestamp
    st = p.simulate([ak.op_create([raw_attr("kind")], min_lifetime=0, expires_at=ts, salt=0xA3)[0]], rec)
    years = (ts - b) * 2 / (365.25 * 86_400)
    ctx["unix"] = {"expiresAt": ts, "head": b, "years": round(years, 1)}
    return ("OBSERVED" if st[0] == "ok" else "NOT REPRODUCED",
            "create with expiresAt=%d (unix time of now+1 day) at head %d: %s %s -> lives ~%.0f years at 2 s/block"
            % (ts, b, st[0], st[1], years))


def equal_expiry_extend(p: Probe, rec, ctx):
    b = p.block(rec)
    n = p.entity_nonce(BEEF, rec)
    target = b + 1000
    create, salt = ak.op_create([raw_attr("kind")], min_lifetime=0, expires_at=target, salt=0xA4)
    key = ak.entity_key(BEEF, n, salt)
    st = p.simulate([create, ak.op_extend(key, min_lifetime=0, expires_at=target)], rec)
    return ("OBSERVED" if st[0] == "ok" else "NOT REPRODUCED",
            "create expiresAt=%d then extend to the same %d in one batch: %s %s" % (target, target, st[0], st[1]))


def expiry_has_no_event(p: Probe, rec, ctx):
    b = p.block(rec)
    sel = {"key": True, "owner": True, "createdAt": True, "expiresAt": True}
    cands = []
    for back in (300, 2000):
        res, err = p.query("$expiresAt <= u64(%d)" % (b - 10), {"atBlock": hex(b - back), "limit": 5, "select": sel},
                           rec)
        cands = sorted((res or {}).get("data") or [], key=lambda r: -int(r["expiresAt"], 16))
        if cands:
            break
    if not cands:
        return "SKIPPED", "no entity expired in the last 2000 blocks"
    keys = [c["key"] for c in cands]
    logs, lerr = p.rpc("eth_getLogs", [{"address": ak.REGISTRY, "topics": [None, keys],
                                        "fromBlock": hex(min(int(c["createdAt"], 16) for c in cands)),
                                        "toBlock": hex(b)}], rec)
    if lerr:
        return "ERROR", "eth_getLogs failed: %s" % short(lerr, 200)
    by_key: dict = {}
    for lg in logs or []:
        by_key.setdefault(lg["topics"][1].lower(), []).append((EVENTS.get(lg["topics"][0], "?"),
                                                               int(lg["blockNumber"], 16)))
    pick = next((c for c in cands if "EntityDeleted" not in [e for e, _ in by_key.get(c["key"].lower(), [])]), None)
    if not pick:
        return "SKIPPED", "every candidate was deleted, not expired"
    key, exp = pick["key"].lower(), int(pick["expiresAt"], 16)
    before, _ = p.rpc("arkiv_getEntity", [key, exp - 1], rec)
    at, _ = p.rpc("arkiv_getEntity", [key, exp], rec)
    now, _ = p.rpc("arkiv_getEntity", [key], rec)
    events = by_key.get(key, [])
    ctx["expired"] = {"key": key, "owner": pick["owner"].lower(), "expiresAt": exp}
    ok = before is not None and at is None and now is None and "EntityCreated" in [e for e, _ in events]         and all(blk < exp for _, blk in events)
    return ("PASS" if ok else "FAIL",
            "entity %s...%s expired at block %d (head %d): getEntity at %d -> %s, at %d -> %s, now -> %s; "
            "its logs: %s (none at or after the expiry block)"
            % (key[:6], key[-4:], exp, b, exp - 1, "entity" if before else "null", exp,
               "entity" if at else "null", "entity" if now else "null", events))


def expired_entity_ops(p: Probe, rec, ctx):
    e = ctx.get("expired")
    if not e:
        return "SKIPPED", "needs the expired entity found by expiry_has_no_event"
    d = p.simulate([ak.op_delete(e["key"])], rec, sender=e["owner"])
    x = p.simulate([ak.op_extend(e["key"], min_lifetime=100)], rec, sender=e["owner"])
    return ("OBSERVED" if d[0] == "ok" else "NOT REPRODUCED",
            "as its owner %s...%s, %d blocks after expiry: delete -> %s %s; extend -> %s %s"
            % (e["owner"][:6], e["owner"][-4:], p.head - e["expiresAt"], d[0], d[1], x[0], x[1]))


def readonly_flag(p: Probe, rec, ctx):
    n = p.entity_nonce(BEEF, rec)
    create, salt = ak.op_create([raw_attr("kind")], min_lifetime=100, flags=ak.READONLY, salt=0xA5)
    key = ak.entity_key(BEEF, n, salt)
    d = p.simulate([create, ak.op_delete(key)], rec)
    pt = p.simulate([create, op_patch(key, [raw_attr("kind", b"y")])], rec)
    ok = d[0] == "ok" and pt[0] == "revert" and "ReadOnlyEntity" in pt[1]
    return ("PASS" if ok else "FAIL",
            "entityNonce(beef)=%d; [create readonly, delete] -> %s %s; [create readonly, patch] -> %s %s"
            % (n, d[0], d[1], pt[0], pt[1]))


def permissionless_extend(p: Probe, rec, ctx):
    bc = ctx.get("burn")
    if not bc:
        return "SKIPPED", "no burn-check evidence file"
    tx, _ = p.rpc("eth_getTransactionByHash", [bc["extend_tx"]], rec)
    rc, _ = p.rpc("eth_getTransactionReceipt", [bc["extend_tx"]], rec)
    ext = [lg for lg in (rc or {}).get("logs", []) if EVENTS.get(lg["topics"][0]) == "ExpiryExtended"]
    if not tx or not ext:
        return "FAIL", "extend tx or its ExpiryExtended log not found"
    lg = ext[0]
    owner_topic, sender = topic_addr(lg["topics"][2]), tx["from"].lower()
    ok = rc["status"] == "0x1" and lg["topics"][1].lower() == bc["key"] and owner_topic == DEAD \
        and sender == bc["third_party"] and sender != bc["creator"]
    return ("PASS" if ok else "FAIL",
            "tx from %s (not the creator %s) extended entity %s...%s to block %d; event owner topic = %s"
            % (sender, bc["creator"], bc["key"][:6], bc["key"][-4:], int(lg["data"], 16), owner_topic))


def calldata_matches_docs(p: Probe, rec, ctx):
    attrs = [ak.text("category", "example"), ak.u64("count", 100)] + ak.payload({"message": "Hello from Arkiv"})
    op, _ = ak.op_create(attrs, min_lifetime=1_296_000, salt=0x1234)
    ours = ak.execute_calldata([op])
    rec.append({"utc": utc(), "method": "(offline) writer.arkiv.execute_calldata",
                "params": "docs example: salt 0x1234, minLifetime 1296000, category=str example, count=u64 100, "
                          "$contentType application/json, $payload {\"message\":\"Hello from Arkiv\"}",
                "result": {"bytes": (len(ours) - 2) // 2, "equal_to_docs": ours == DOCS_EXAMPLE_CALLDATA,
                           "sha256": __import__("hashlib").sha256(bytes.fromhex(ours[2:])).hexdigest()}})
    return ("PASS" if ours == DOCS_EXAMPLE_CALLDATA else "FAIL",
            "%d-byte calldata from eth_abi %s %s the docs' full calldata" % (
                (len(ours) - 2) // 2, md.version("eth_abi"), "equals" if ours == DOCS_EXAMPLE_CALLDATA else "differs from"))


def key_prediction(p: Probe, rec, ctx):
    bc = ctx.get("burn")
    if not bc:
        return "SKIPPED", "no burn-check evidence file"
    tx, _ = p.rpc("eth_getTransactionByHash", [bc["create_tx"]], rec)
    rc, _ = p.rpc("eth_getTransactionReceipt", [bc["create_tx"]], rec)
    ops = abi_decode(["(uint8,bytes)[]"], bytes.fromhex(tx["input"][10:]))[0]
    salt = abi_decode(["(uint128,uint64,uint64,uint8,(bytes32,uint8,bytes)[])"], ops[0][1])[0][0]
    created = [lg["topics"][1].lower() for lg in rc["logs"] if EVENTS.get(lg["topics"][0]) == "EntityCreated"]
    blk = int(rc["blockNumber"], 16)
    n = p.entity_nonce(tx["from"], rec, hex(blk - 1))
    predicted = ak.entity_key(tx["from"].lower(), n, salt)
    ok = created == [predicted]
    return ("PASS" if ok else "FAIL",
            "tx in block %d: salt %s, entityNonce(sender) at block %d = %d, predicted %s...%s, EntityCreated %s"
            % (blk, hex(salt), blk - 1, n, predicted[:6], predicted[-4:], "matches" if ok else created))


def browser_access(p: Probe, rec, ctx):
    req = urllib.request.Request(p.url, method="OPTIONS", headers={
        "Origin": ORIGIN, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type",
        "User-Agent": UA})
    status, headers, _ = p._http(req)
    p.calls += 1
    cors = {k.lower(): v for k, v in headers.items() if k.lower().startswith("access-control-")}
    rec.append({"utc": utc(), "method": "HTTP OPTIONS (CORS preflight)", "params": {"Origin": ORIGIN}, "http": status,
                "headers": cors})
    try:
        from websockets.sync.client import connect
    except ImportError:
        return "SKIPPED", "CORS preflight %s; python package websockets not installed" % status
    got = {"newHeads": 0, "logs": 0}
    subs: dict = {}
    t0, ws_rec = time.time(), {"utc": utc(), "method": "WSS eth_subscribe newHeads + logs(address=0x44..44)",
                               "params": {"url": WSS_URL, "Origin": ORIGIN}}
    p.calls += 1
    try:
        with connect(WSS_URL, origin=ORIGIN, open_timeout=15) as ws:
            ws.send(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "eth_subscribe", "params": ["newHeads"]}))
            ws.send(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "eth_subscribe",
                                "params": ["logs", {"address": ak.REGISTRY}]}))
            while time.time() - t0 < 25 and not (got["newHeads"] and got["logs"]):
                try:
                    m = json.loads(ws.recv(timeout=5))
                except TimeoutError:
                    continue
                if m.get("id") in (1, 2):
                    subs[m["result"] if "result" in m else "error"] = "newHeads" if m["id"] == 1 else "logs"
                    ws_rec.setdefault("subscribe_replies", []).append(short(m, 200))
                elif m.get("method") == "eth_subscription":
                    kind = subs.get(m["params"]["subscription"], "?")
                    if kind in got:
                        if not got[kind]:
                            res = m["params"]["result"]
                            ws_rec.setdefault("first", {})[kind] = (
                                {"number": res.get("number")} if kind == "newHeads"
                                else {"event": EVENTS.get(res["topics"][0], "?"), "block": res.get("blockNumber")})
                        got[kind] += 1
    except Exception as e:  # noqa: BLE001 - record any transport failure as the result
        ws_rec["error"] = "%s: %s" % (type(e).__name__, e)
    ws_rec["received"] = got
    ws_rec["seconds"] = round(time.time() - t0, 1)
    rec.append(ws_rec)
    ok = cors.get("access-control-allow-origin") in ("*", ORIGIN) and got["newHeads"] > 0 and got["logs"] > 0
    return ("PASS" if ok else "FAIL",
            "CORS preflight %s, allow-origin %s; WSS with Origin %s: %d newHeads and %d logs in %.0f s"
            % (status, cors.get("access-control-allow-origin"), ORIGIN, got["newHeads"], got["logs"],
               ws_rec["seconds"]))


def rate_headers(p: Probe, rec, ctx):
    table: dict = {}
    for r in p.records:
        t = table.setdefault(r["method"], {"calls": 0, "arkiv-cost": set(), "ratelimit": []})
        t["calls"] += 1
        t["arkiv-cost"].add(r["headers"].get("arkiv-cost", "absent"))
        if r["headers"].get("ratelimit"):
            t["ratelimit"].append(r["headers"]["ratelimit"])
    out = {m: {"calls": t["calls"], "arkiv-cost": sorted(t["arkiv-cost"]),
               "ratelimit_first_last": [t["ratelimit"][0], t["ratelimit"][-1]] if t["ratelimit"] else []}
           for m, t in table.items()}
    limits = sorted({h.split(",")[0] for r in p.records for h in [r["headers"].get("ratelimit", "")] if h})
    remaining = [int(h.split("remaining=")[1].split(",")[0]) for r in p.records
                 for h in [r["headers"].get("ratelimit", "")] if "remaining=" in h]
    expose = next((r["headers"]["access-control-expose-headers"] for r in p.records
                   if r["headers"].get("access-control-expose-headers")), None)
    rec.append({"utc": utc(), "method": "(aggregate of this run's response headers)",
                "result": {"per_method": out, "ratelimit_limits_seen": limits, "remaining_sequence": remaining,
                           "access-control-expose-headers": expose}})
    costed = sorted(m for m, t in out.items() if t["arkiv-cost"] != ["absent"])
    return ("THROTTLED" if p.throttled else "OBSERVED",
            "arkiv-cost header only on: %s; ratelimit %s on every method, remaining went %s; %d requests, "
            "%d arkiv_query; %s"
            % (", ".join("%s=%s" % (m, "/".join(out[m]["arkiv-cost"])) for m in costed) or "none",
               "/".join(limits) or "absent", remaining, p.calls, p.queries, p.throttled or "no 429"))


def default_user_agent(p: Probe, rec, ctx):
    body = json.dumps({"jsonrpc": "2.0", "id": 0, "method": "eth_blockNumber", "params": []}).encode()
    got = {}
    for label, ua in (("urllib default", None), ("custom", UA)):
        headers = {"Content-Type": "application/json"}
        if ua:
            headers["User-Agent"] = ua
        status, hdrs, raw = p._http(urllib.request.Request(p.url, body, headers))
        p.calls += 1
        sent = ua or "Python-urllib/%d.%d" % sys.version_info[:2]
        text = raw[:120].decode("utf-8", "replace").strip()
        rec.append({"utc": utc(), "method": "eth_blockNumber (plain HTTP POST)", "params": {"User-Agent": sent},
                    "http": status, "headers": {"server": hdrs.get("server")}, "body": text})
        got[label] = (status, sent, text)
    ok = got["urllib default"][0] == 403 and got["custom"][0] == 200
    return ("OBSERVED" if ok else "NOT REPRODUCED",
            "same eth_blockNumber body: User-Agent %s -> HTTP %d %r; User-Agent %s -> HTTP %d"
            % (got["urllib default"][1], got["urllib default"][0], got["urllib default"][2], got["custom"][1],
               got["custom"][0]))


ITEMS = [
    ("untagged_number", "friction", "Untagged number against a u64 attribute: silent 0 rows", untagged_number),
    ("uppercase_name", "friction", "Uppercase attribute name: write reverts, query silently empty", uppercase_name),
    ("reserved_word_name", "friction", "Reserved words accepted as attribute names on write", reserved_word_name),
    ("cursor_without_atblock", "friction", "Cursor from the docs' pagination example fails once the head moves",
     cursor_without_atblock),
    ("block_param_encoding", "friction", "Block parameter encodings differ across the read methods",
     block_param_encoding),
    ("updated_at_queryable", "friction", "$updatedAt: fundamentals says queryable, node says not",
     updated_at_queryable),
    ("unix_seconds_expiry", "friction", "Unix seconds in expiresAt accepted without a bound", unix_seconds_expiry),
    ("equal_expiry_extend", "friction", "Extend to the same expiry: docs say revert, node accepts",
     equal_expiry_extend),
    ("expiry_has_no_event", "documented", "Expiry emits no event; reads hide the entity at expiresAt",
     expiry_has_no_event),
    ("expired_entity_ops", "friction", "Delete of an expired entity is accepted", expired_entity_ops),
    ("readonly_flag", "documented", "Read-only blocks patch, not delete", readonly_flag),
    ("permissionless_extend", "documented", "Third-party extension; event owner topic is the entity owner",
     permissionless_extend),
    ("calldata_matches_docs", "documented", "execute() calldata from eth_abi equals the docs example",
     calldata_matches_docs),
    ("key_prediction", "documented", "Entity key formula reproduces a live key", key_prediction),
    ("browser_access", "documented", "CORS and WebSocket subscriptions from a web origin", browser_access),
    ("default_user_agent", "friction", "Python's default urllib User-Agent is refused with HTTP 403",
     default_user_agent),
    ("rate_headers", "friction", "Public RPC cost headers versus the documented 'default rate limit'", rate_headers),
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--only", help="comma-separated item ids (no evidence file unless --out)")
    ap.add_argument("--out", help="evidence path (default arkiv/evidence/friction-<utc date>.json for a full run)")
    a = ap.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):      # node messages contain non-ASCII; never crash a console on them
        sys.stdout.reconfigure(errors="backslashreplace")
    only = set(a.only.split(",")) if a.only else None
    p = Probe(ak.RPC_URL)
    started = utc()
    hdr: list = []
    version, _ = p.rpc("web3_clientVersion", [], hdr)
    chain, _ = p.rpc("eth_chainId", [], hdr)
    head0 = p.block(hdr)
    print("node %s, chain %d, head %d, %s" % (version, int(chain, 16), head0, started))
    ctx: dict = {"burn": burn_check()}
    items = []
    for item_id, kind, title, fn in ITEMS:
        if only and item_id not in only and item_id != "rate_headers":
            continue
        rec: list = []
        t = utc()
        try:
            status, summary = fn(p, rec, ctx)
        except Throttled as e:
            status, summary = "SKIPPED", "query budget: %s" % e
        except Exception as e:  # noqa: BLE001 - one broken item must not lose the others' evidence
            status, summary = "ERROR", "%s: %s" % (type(e).__name__, e)
        print("[%s] %s  %s\n      %s" % (item_id, status, title, summary))
        items.append({"id": item_id, "kind": kind, "title": title, "status": status, "summary": summary,
                      "utc": t, "head": p.head, "node_version": version, "calls": rec})
    out = {"probe": "tools/friction_probe.py", "utc_start": started, "utc_end": utc(), "rpc": p.url, "wss": WSS_URL,
           "chain_id": int(chain, 16), "node_version": version, "head_start": head0, "head_end": p.head,
           "client": {"python": platform.python_version(), "eth_abi": md.version("eth_abi"),
                      "eth_account": md.version("eth_account"), "sdk": "none (direct JSON-RPC)"},
           "docs": DOCS, "burn_check_evidence": (ctx["burn"] or {}).get("file"),
           "counts": {"requests": p.calls, "arkiv_query": p.queries}, "setup_calls": hdr, "items": items}
    path = a.out or (None if only else os.path.join(
        ROOT, "arkiv", "evidence", "friction-%s.json" % dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")))
    if path:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(out, f, indent=1, default=str)
            f.write("\n")
        print("evidence: %s" % os.path.relpath(path, ROOT).replace(os.sep, "/"))
    print("requests %d, arkiv_query %d%s" % (p.calls, p.queries, "; " + p.throttled if p.throttled else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
