"""Plant a deliberately fake halt from a wallet that is NOT an approved reporter.

The rows look exactly like real ones (same app/kind/venue/route/side attributes). Only $creator, which
the chain sets and nobody can forge, tells them apart. The reader hides them while its reporter
filter is on and shows them, tagged "unapproved creator", when it is off.

    python tools/plant_spoof.py --dry-run
    python tools/plant_spoof.py            # sends one transaction from the 'spoof' wallet
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from writer import arkiv as ak  # noqa: E402

DAY = 43_200
NOTE = "planted by the Door Ledger team to demonstrate filtering by $creator; not a real observation"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    w = ak.Writer(ak.load_account("spoof"))
    now = int(time.time())
    t0 = now - 5 * 3600
    common = [ak.text("app", "doorledger"), ak.text("venue", "binance"), ak.text("route", "ETH"),
              ak.text("net", "ethereum"), ak.text("side", "withdraw")]
    lease, _ = ak.op_create(common + [ak.text("kind", "lease"), ak.u64("t0", t0)] + ak.payload(
        {"v": 1, "venue": "binance", "route": "ETH", "net": "ethereum", "side": "withdraw", "t0": t0,
         "listed": 40, "closed": 40, "assets": ["ETH", "USDT", "USDC"], "note": NOTE}),
        min_lifetime=14 * DAY, flags=ak.READONLY)
    t0e, t1e = now - 3 * DAY, now - 3 * DAY + 9 * 3600
    episode, _ = ak.op_create(common + [ak.text("kind", "episode"), ak.u64("t0", t0e), ak.u64("t1", t1e),
                                        ak.u64("dur_s", t1e - t0e)] + ak.payload(
        {"v": 1, "venue": "binance", "route": "ETH", "net": "ethereum", "side": "withdraw", "t0": t0e, "t1": t1e,
         "dur_s": t1e - t0e, "note": NOTE}), min_lifetime=180 * DAY, flags=ak.READONLY)
    ops = [lease, episode]
    gas = w.simulate(ops)
    if a.dry_run:
        print(json.dumps({"spoof": w.addr, "gas": gas}))
        return 0
    rc = w.send(ops)
    print(json.dumps({"spoof": w.addr, "tx": rc.tx, "block": rc.block, "created": rc.created}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
