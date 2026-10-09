"""Run a Door Ledger reporter.

    # long-running watcher (a server): poll every 120 s, 30-min leases renewed every ~10 min
    python -m writer.run --role reporter1 --loop

    # one-shot watcher (a scheduled job, e.g. GitHub Actions): 3 polls 60 s apart, 4-hour leases,
    # its open halts are recovered from its own leases on Arkiv, so it needs no local state
    python -m writer.run --role reporter2 --once

    # read-only: poll the venues and print what the rule sees; nothing is signed
    python -m writer.run --dry-run

The signing key is read from ~/.arkiv-<role>.env (line ARKIV_SIGNER_HEX=0x...) or ARKIV_KEY_FILE.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import signal
import sys
import time

from . import arkiv as ak
from . import engine
from . import venues as vn


def log(msg: str) -> None:
    print("%s %s" % (dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), msg), flush=True)


def dry_run() -> int:
    for v in vn.VENUES:
        snap = vn.poll(v)
        found, counts, _ = vn.halts(snap)
        log("%s %s %s" % (v, "ok" if snap.ok else snap.error, counts))
        for h in found:
            log("  halted %s %s (%s) %d/%d shut: %s" % (h.route, h.side, h.net, len(h.closed), h.listed,
                                                         ", ".join(h.closed[:8])))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Door Ledger reporter")
    ap.add_argument("--role", default="reporter1")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--loop", action="store_true")
    mode.add_argument("--once", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    ap.add_argument("--interval", type=int, default=120, help="seconds between polls (loop)")
    ap.add_argument("--polls", type=int, default=3, help="polls per run (once)")
    ap.add_argument("--spacing", type=int, default=60, help="seconds between polls (once)")
    ap.add_argument("--state", default=None, help="local state file (loop); default state/<role>.json")
    a = ap.parse_args(argv)
    if a.dry_run:
        return dry_run()

    writer = ak.Writer(ak.load_account(a.role), log=log)
    bal = writer.rpc.balance(writer.addr)
    log("reporter %s %s balance %.4f GLM code %s" % (a.role, writer.addr, bal / 1e18, engine.code_version()))
    if a.loop:
        state = a.state or os.path.join("state", "%s.json" % a.role)
        rep = engine.Reporter(writer, lease_life=900, pulse_every_s=3600, pulse_life=4500, state_path=state, log=log)
        rep.load()
        stop = []
        signal.signal(signal.SIGTERM, lambda *_: stop.append(1))
        while not stop:
            started = time.time()
            try:
                rep.cycle()
            except Exception as e:  # noqa: BLE001 - one bad cycle must not kill the watcher
                log("cycle error: %s: %s" % (type(e).__name__, e))
                rep.save()
            while not stop and time.time() - started < a.interval:
                time.sleep(1)
        rep.save()
        log("stopped; state saved")
        return 0
    rep = engine.Reporter(writer, lease_life=7200, pulse_every_s=0, pulse_life=5400, sampled=True, log=log)
    rep.load()
    failed = 0
    for i in range(a.polls):
        try:
            to_open, to_close = rep.observe()
            if rep.resolve_pending():
                rep.write_closes(to_close)
                rep.write_opens(to_open)
        except Exception as e:  # noqa: BLE001 - keep going: the heartbeat and pulses still matter
            failed += 1
            log("poll %d error: %s: %s" % (i + 1, type(e).__name__, e))
        if i < a.polls - 1:
            time.sleep(a.spacing)
    try:
        if rep.resolve_pending():
            rep.write_heartbeat()
            rep.write_pulses(force=True)
    except Exception as e:  # noqa: BLE001
        failed += 1
        log("heartbeat error: %s: %s" % (type(e).__name__, e))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
