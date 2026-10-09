"""A second consumer of the Door Ledger data, written only from arkiv/schema.md.

It uses no Door Ledger code, no server of ours and no third-party package: just the public Arkiv
node and the Python standard library. It shows what another team can build on the dataset without
asking anyone: a terminal view of open and closed halts, and a follower that prints new halts and
reopenings as they land on chain.

    python tools/consumer_example.py                       # open halts and the 10 latest closed ones
    python tools/consumer_example.py --follow              # then keep watching, every 2 minutes
    python tools/consumer_example.py --watchers 0xabc...,0xdef...   # trust your own list of watchers
    python tools/consumer_example.py --venue binance --min-hours 6  # filter closed halts
"""
import argparse
import datetime as dt
import json
import time
import urllib.request

RPC = "https://rpc.tiramisu.db-chain.testnet.arkiv.network"
DEFAULT_WATCHERS = ["0x65ebe97db5cd7160bf9a2aa7818241f9e5768a92", "0xc10547dbac4e57b89f0f186d79c3a70b1fe533fb"]
SELECT = {"key": True, "creator": True, "owner": True, "attributes": True}


def query(q, limit=50):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "arkiv_query",
                       "params": [q, {"limit": limit, "select": SELECT}]}).encode()
    req = urllib.request.Request(RPC, body, {"Content-Type": "application/json",
                                             "User-Agent": "door-ledger-consumer-example/1"})
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.load(r)
    if "error" in out:
        raise SystemExit("arkiv_query error: %s" % out["error"].get("message"))
    return out["result"]["data"]


def attrs(row):
    out = {}
    for a in row.get("attributes") or []:
        v = a["value"]
        out[a["name"]] = int(v, 16) if a["type"] == "u64" and isinstance(v, str) else v
    return out


def when(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watchers", help="comma-separated watcher addresses to trust (default: the two Door Ledger watchers)")
    ap.add_argument("--venue", choices=["binance", "gate", "kucoin", "bithumb"])
    ap.add_argument("--min-hours", type=float, default=0)
    ap.add_argument("--follow", action="store_true")
    ap.add_argument("--every", type=int, default=120, help="seconds between checks with --follow")
    a = ap.parse_args()
    watchers = [w.strip().lower() for w in (a.watchers.split(",") if a.watchers else DEFAULT_WATCHERS)]
    trusted = "(" + " OR ".join("$creator = addr(%s)" % w for w in watchers) + ")"
    base = "app = str('doorledger')"
    hist_q = base + " AND kind = str('episode') AND " + trusted
    if a.venue:
        hist_q += " AND venue = str('%s')" % a.venue
    if a.min_hours:
        hist_q += " AND dur_s >= u64(%d)" % int(a.min_hours * 3600)
    open_q = base + " AND kind = str('lease') AND " + trusted

    def snapshot():
        halts = {}
        for row in query(open_q, 200):
            x = attrs(row)
            halts.setdefault((x["venue"], x["route"], x["side"]), []).append((x["t0"], row["creator"]))
        return halts, query(hist_q, 10)

    halts, closed = snapshot()
    print("Open now (trusting %d watcher(s)):" % len(watchers))
    for (venue, route, side), seen in sorted(halts.items()):
        print("  %-8s %-12s %-8s first seen shut %s, confirmed by %d watcher(s)" % (
            venue, route, side, when(min(t for t, _ in seen)), len({c for _, c in seen})))
    if not halts:
        print("  none")
    print("Closed halts (newest first):")
    for row in closed:
        x = attrs(row)
        locked = "locked" if row["owner"].lower().endswith("dead") else "owner " + row["owner"]
        print("  %-8s %-12s %-8s %s to %s (%.1f h), %s" % (x["venue"], x["route"], x["side"], when(x["t0"]),
                                                          when(x["t1"]), x["dur_s"] / 3600, locked))
    if not closed:
        print("  none yet")
    seen_closed = {row["key"] for row in closed}
    while a.follow:
        time.sleep(a.every)
        now_halts, now_closed = snapshot()
        for k in sorted(set(now_halts) - set(halts)):
            print("%s NEW HALT  %s %s %s" % (when(time.time()), *k))
        for row in now_closed:
            if row["key"] not in seen_closed:
                x = attrs(row)
                print("%s REOPENED %s %s %s after %.1f h" % (when(time.time()), x["venue"], x["route"], x["side"],
                                                             x["dur_s"] / 3600))
                seen_closed.add(row["key"])
        halts = now_halts


if __name__ == "__main__":
    main()
