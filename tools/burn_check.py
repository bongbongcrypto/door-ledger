"""Live check of the property the ledger's closed records rely on: once a record is moved to 0x...dEaD,
its creator can no longer delete or edit it, while anyone can still extend its life.

    python tools/burn_check.py            # writes one 'selftest' entity from reporter1, then checks
    python tools/burn_check.py --dry-run  # simulations only from a dummy address: no key, nothing sent
    python tools/burn_check.py --episode 0x...   # a real closed record: no key, nothing sent

Steps (all on Tiramisu):
 1. reporter1 sends one atomic batch: create(kind=selftest, flags=readonly|permissionless-extension) + transfer to dEaD.
 2. Read the entity back: $owner must be dEaD and $creator must be reporter1.
 3. Simulate delete and transfer from reporter1: both must revert NotOwner.
 4. A different wallet (the 'spoof' wallet) extends the entity for real: it must succeed.
The result is written to arkiv/evidence/burn-check-<utc date>.json.

--episode KEY checks a closed halt a watcher actually wrote: it reads the record, finds the transaction that
created it and lists what else that transaction did, then simulates (eth_estimateGas, which needs no key) the
creator deleting it or taking it back, which must revert NotOwner, and a stranger extending it, which must pass.
The result is written to arkiv/evidence/episode-<first 8 hex of the key>.json.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from writer import arkiv as ak  # noqa: E402

DAY = 43_200  # blocks at 2 s


class _Sim:
    """A key-less stand-in for ak.Writer that can only simulate."""

    def __init__(self, rpc, addr):
        self.rpc, self.addr = rpc, addr

    def simulate(self, ops):
        return ak.Writer.simulate(self, ops)


EVENTS = {ak.TOPIC_CREATED: "created", ak.TOPIC_TRANSFERRED: "transferred", ak.TOPIC_DELETED: "deleted",
          ak.TOPIC_EXTENDED: "extended"}


def check_episode(rpc, key: str) -> int:
    key = key.lower()
    e = rpc.call("arkiv_getEntity", [key])
    num = lambda v: int(v, 16) if isinstance(v, str) else v  # noqa: E731
    attrs = {x["name"]: (num(x["value"]) if x["type"] == "u64" else x["value"]) for x in e.get("attributes") or []}
    flags = (e.get("creationFlags") or {}).get("raw")
    out = {"utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "chain_id": ak.CHAIN_ID,
           "key": key, "creator": e["creator"], "owner": e["owner"], "flags": flags,
           "created_at_block": num(e["createdAt"]), "expires_at_block": num(e["expiresAt"]), "attributes": attrs,
           "steps": []}
    out["steps"].append({"step": "owner is 0x...dEaD, read-only and permissionless extension",
                         "ok": e["owner"].lower() == ak.BURN.lower() and flags == ak.READONLY | ak.PERMISSIONLESS_EXTENSION})
    block = hex(num(e["createdAt"]))
    logs = rpc.call("eth_getLogs", [{"address": ak.REGISTRY, "fromBlock": block, "toBlock": block,
                                     "topics": [ak.TOPIC_CREATED, key]}])
    tx = logs[0]["transactionHash"] if logs else None
    if tx:
        r = rpc.call("eth_getTransactionReceipt", [tx])
        events = [{"event": EVENTS.get(lg["topics"][0], lg["topics"][0]), "key": lg["topics"][1]}
                  for lg in r["logs"] if lg["address"].lower() == ak.REGISTRY.lower()]
        names = [ev["event"] for ev in events]
        out["steps"].append({"step": "one transaction created it, handed it to 0x...dEaD and deleted the lease",
                             "tx": tx, "from": r["from"], "status": r["status"], "events": events,
                             "ok": r["status"] == "0x1" and names[:2] == ["created", "transferred"]
                             and events[1]["key"] == key and "deleted" in names})
    else:
        out["steps"].append({"step": "find the creating transaction", "ok": False})
    creator = _Sim(rpc, e["creator"])
    stranger = _Sim(rpc, "0x000000000000000000000000000000000000bEEF")
    # min_lifetime counts from now, so it has to reach past the record's current 180-day expiry to extend it.
    past_expiry = num(e["expiresAt"]) - int(rpc.call("eth_blockNumber", []), 16) + 14 * DAY
    for label, who, op, want in (("creator deletes it", creator, ak.op_delete(key), "NotOwner"),
                                 ("creator takes it back", creator, ak.op_transfer(key, e["creator"]), "NotOwner"),
                                 ("a stranger extends it 14 days past its current expiry", stranger,
                                  ak.op_extend(key, past_expiry), "ok")):
        try:
            g = who.simulate([op])
            out["steps"].append({"step": label, "result": "allowed, gas %d" % g, "ok": want == "ok"})
        except ak.ArkivError as err:
            out["steps"].append({"step": label, "result": str(err)[:160], "ok": want in str(err)})
    out["passed"] = all(st["ok"] for st in out["steps"])
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arkiv", "evidence")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "episode-%s.json" % key[2:10]), "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, indent=1)
        f.write("\n")
    print(json.dumps(out, indent=1))
    return 0 if out["passed"] else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--episode", metavar="KEY", help="check a real closed record instead (no key needed)")
    a = ap.parse_args(argv)
    rpc = ak.Rpc()
    if a.episode:
        return check_episode(rpc, a.episode)
    if a.dry_run:                      # simulation only: any address works, no key is read
        me = other = _Sim(rpc, "0x000000000000000000000000000000000000bEEF")
    else:
        me = ak.Writer(ak.load_account("reporter1"), rpc)
        other = ak.Writer(ak.load_account("spoof"), rpc)
    out = {"utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "chain_id": ak.CHAIN_ID,
           "creator": me.addr, "burn": ak.BURN, "third_party": other.addr, "steps": []}

    nonce = rpc.entity_nonce(me.addr)
    attrs = [ak.text("app", "doorledger"), ak.text("kind", "selftest"), ak.text("check", "burn-lock")]
    attrs += ak.payload({"v": 1, "note": "Door Ledger self-test: a burned record cannot be deleted by its creator"})
    create, salt = ak.op_create(attrs, min_lifetime=7 * DAY, flags=ak.READONLY | ak.PERMISSIONLESS_EXTENSION)
    key = ak.entity_key(me.addr, nonce, salt)
    batch = [create, ak.op_transfer(key, ak.BURN)]
    gas = me.simulate(batch)
    out["steps"].append({"step": "simulate create+transfer", "gas": gas, "predicted_key": key})
    if a.dry_run:
        # The same lock, proven without a key: in one simulated batch the caller creates the entity,
        # hands it to dEaD, then tries to delete it (must revert NotOwner) or extend it (must pass).
        for label, tail, want in (("create+transfer, then the creator deletes", ak.op_delete(key), "NotOwner"),
                                  ("create+transfer, then a non-owner extends", ak.op_extend(key, 14 * DAY), "ok")):
            try:
                g = me.simulate(batch + [tail])
                out["steps"].append({"step": label, "result": "ok, gas %d" % g, "ok": want == "ok"})
            except ak.ArkivError as e:
                out["steps"].append({"step": label, "result": str(e)[:160], "ok": want in str(e)})
        out["passed"] = all(st.get("ok", True) for st in out["steps"])
        print(json.dumps(out, indent=1))
        return 0 if out["passed"] else 1

    rc = me.send(batch)
    out["steps"].append({"step": "create+transfer", "tx": rc.tx, "block": rc.block, "gas_used": rc.gas_used,
                         "created": rc.created})
    if rc.created != [key]:
        out["error"] = "created key %s differs from predicted %s" % (rc.created, key)
        return finish(out, 1)

    rows = rpc.query("$key = key(%s)" % key, select={"key": True, "owner": True, "creator": True,
                                                     "expiresAt": True, "attributes": True})["data"]
    row = rows[0] if rows else {}
    ok_read = bool(row) and row["owner"].lower() == ak.BURN.lower() and row["creator"].lower() == me.addr.lower()
    out["steps"].append({"step": "read back", "owner": row.get("owner"), "creator": row.get("creator"),
                         "expiresAt": row.get("expiresAt"), "ok": ok_read})

    for label, op in (("creator deletes", ak.op_delete(key)), ("creator transfers back", ak.op_transfer(key, me.addr))):
        try:
            me.simulate([op])
            out["steps"].append({"step": label, "result": "ALLOWED", "ok": False})
        except ak.ArkivError as e:
            out["steps"].append({"step": label, "result": str(e), "ok": "NotOwner" in str(e)})

    before = int(row.get("expiresAt", "0x0"), 16) if isinstance(row.get("expiresAt"), str) else row.get("expiresAt")
    try:
        rc2 = other.send([ak.op_extend(key, min_lifetime=14 * DAY)])
        after = rpc.query("$key = key(%s)" % key, select={"key": True, "expiresAt": True})["data"][0]["expiresAt"]
        out["steps"].append({"step": "third party extends", "tx": rc2.tx, "expiresAt_before": before,
                             "expiresAt_after": int(after, 16), "ok": int(after, 16) > (before or 0)})
    except ak.ArkivError as e:
        out["steps"].append({"step": "third party extends", "result": str(e), "ok": False})

    return finish(out, 0 if all(s.get("ok", True) for s in out["steps"]) else 1)


def finish(out: dict, code: int) -> int:
    out["passed"] = code == 0
    d = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "arkiv", "evidence")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "burn-check-%s.json" % out["utc"][:10])
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, indent=1)
        f.write("\n")
    print(json.dumps(out, indent=1))
    return code


if __name__ == "__main__":
    sys.exit(main())
