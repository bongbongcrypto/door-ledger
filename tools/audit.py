"""Audit everything Door Ledger has written to Arkiv, from the chain alone (read-only, no key).

    python tools/audit.py            # exit 0 when no check fails

Checks:
  - no approved watcher holds two live leases for the same halt (venue, route, side);
  - every episode from an approved watcher is owned by 0x...dEaD (burned) and read-only;
  - no episode's lease is still alive (the close batch deletes it);
  - every approved watcher has a live pulse for every venue (a missing one is reported as silent);
  - lease and episode attributes are well formed (t0 <= t1, dur_s = t1 - t0).
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from writer import arkiv as ak  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
VENUES = ("binance", "gate", "kucoin", "bithumb")
SELECT = {"key": True, "creator": True, "owner": True, "createdAt": True, "expiresAt": True, "attributes": True,
          "payload": True}


def load_reporters() -> tuple[set, dict]:
    with open(os.path.join(HERE, "..", "arkiv", "reporters.json"), encoding="utf-8") as f:
        rs = json.load(f)["reporters"]
    return {r["address"].lower() for r in rs if r["approved"]}, {r["address"].lower(): r["label"] for r in rs}


def fetch(rpc: ak.Rpc, q: str) -> tuple[list, int]:
    rows, cursor, at = [], None, None
    for _ in range(50):
        res = rpc.query(q, at_block=at, limit=200, cursor=cursor, select=SELECT)
        rows += res.get("data") or []
        at = int(res["blockNumber"], 16)
        cursor = res.get("cursor")
        if not cursor:
            return rows, at
    return rows, at


def main() -> int:
    approved, labels = load_reporters()
    rpc = ak.Rpc()
    live, head = fetch(rpc, "app = str('doorledger') AND (kind = str('lease') OR kind = str('pulse'))")
    episodes, _ = fetch(rpc, "app = str('doorledger') AND kind = str('episode')")
    fails, warns = [], []
    leases = {}
    pulses = {}
    for row in live:
        a, creator = ak.attrs_of(row), row["creator"].lower()
        if a.get("kind") == "lease":
            leases.setdefault((creator, a.get("venue"), a.get("route"), a.get("side")), []).append(row)
        else:
            pulses.setdefault((creator, a.get("venue")), []).append(row)
    for (creator, venue, route, side), rows in sorted(leases.items()):
        if creator in approved and len(rows) > 1:
            fails.append("duplicate live leases for %s %s:%s:%s: %s" % (
                labels.get(creator, creator), venue, route, side, ", ".join(r["key"][:10] for r in rows)))
    live_keys = {r["key"].lower() for r in live}
    n_burned = 0
    for row in episodes:
        a, p, creator = ak.attrs_of(row), ak.payload_of(row) or {}, row["creator"].lower()
        name = "%s %s:%s:%s t0=%s" % (labels.get(creator, creator[:10]), a.get("venue"), a.get("route"), a.get("side"),
                                       a.get("t0"))
        if creator not in approved:
            continue
        if row["owner"].lower() != ak.BURN.lower():
            fails.append("episode not burned (owner %s): %s" % (row["owner"], name))
        else:
            n_burned += 1
        if p.get("lease_key", "").lower() in live_keys:
            fails.append("episode's lease is still alive: %s" % name)
        t0, t1, dur = a.get("t0", 0), a.get("t1", 0), a.get("dur_s", -1)
        if not (0 < t0 <= t1) or dur != t1 - t0:
            fails.append("bad times on episode %s: t0=%s t1=%s dur_s=%s" % (name, t0, t1, dur))
    for creator in sorted(approved):
        for venue in VENUES:
            if not pulses.get((creator, venue)):
                warns.append("no live pulse from %s for %s (watcher silent there)" % (labels.get(creator, creator), venue))
    now = time.time()
    for (creator, venue, route, side), rows in leases.items():
        for r in rows:
            t0 = ak.attrs_of(r).get("t0", 0)
            if t0 > now + 300:
                fails.append("lease t0 in the future: %s %s:%s:%s" % (labels.get(creator, creator), venue, route, side))
    approved_leases = sum(len(v) for k, v in leases.items() if k[0] in approved)
    print("head %d | live leases %d (approved %d) | pulses %d | episodes %d (approved burned %d)" % (
        head, sum(len(v) for v in leases.values()), approved_leases, sum(len(v) for v in pulses.values()),
        len(episodes), n_burned))
    for creator in sorted(approved):
        mine = sorted("%s:%s:%s" % (v, r, s) for (c, v, r, s) in leases if c == creator)
        print("  %s open: %s" % (labels.get(creator, creator), ", ".join(mine) or "none"))
    for w in warns:
        print("WARN", w)
    for f in fails:
        print("FAIL", f)
    print("audit: %s" % ("PASS" if not fails else "%d FAIL" % len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
