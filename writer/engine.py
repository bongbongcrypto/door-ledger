"""The reporter: turns venue polls into Arkiv entities.

Entity kinds (all carry app='doorledger'):
  lease    an open halt as this reporter sees it now. Short life, renewed while the halt is confirmed;
           if the reporter dies the lease lapses by itself and the halt leaves "Open now".
  episode  a closed halt (the ledger row). Read-only + extendable by anyone, 180 days, and moved to
           0x...dEaD in the same transaction, so its creator can no longer delete or edit it.
  pulse    one per venue per tick: proof that the reporter is alive and could read the venue.

Confirmation: a halt opens after OPEN_POLLS consecutive halted polls and closes after CLOSE_POLLS
consecutive polls that judge the route and find it open. A poll that cannot judge a route (venue
down, short response, route missing) changes nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass, field

from . import arkiv as ak
from . import venues as vn

APP = "doorledger"
OPEN_POLLS = 2
CLOSE_POLLS = 3
EPISODE_LIFE = 7_776_000      # blocks, 180 days at 2 s
SHORT_RESPONSE = 0.9          # a response with fewer items than this share of the venue's max is ignored
STALE_S = 30 * 60             # stop renewing a venue's leases when it has not been read for this long
MAX_ASSETS_IN_PAYLOAD = 20


@dataclass
class Open:
    venue: str
    route: str
    net: str
    side: str
    t0: int
    lease_key: str = ""
    lease_tx: str = ""
    expires_at: int = 0
    listed: int = 0
    max_closed: int = 0
    assets: list = field(default_factory=list)
    eta_ms: int = 0
    polls: int = 0
    gaps: list = field(default_factory=list)
    first_sha256: str = ""
    last_sha256: str = ""
    resumed_from: str = ""
    gap_since: int = 0
    seen_at: int = 0            # last poll (watcher clock) that saw the halt

    @property
    def id(self) -> tuple:
        return (self.venue, self.route, self.side)


def code_version() -> str:
    v = os.environ.get("DOORLEDGER_CODE")
    if v:
        return v[:12]
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        return subprocess.run(["git", "-C", here, "rev-parse", "--short=12", "HEAD"], capture_output=True,
                              text=True, timeout=5).stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


class Reporter:
    def __init__(self, writer: ak.Writer, venues=vn.VENUES, lease_life: int = 900, pulse_every_s: int = 3600,
                 pulse_life: int = 4500, state_path: str | None = None, poll=vn.poll, now=time.time, log=print):
        self.w, self.rpc = writer, writer.rpc
        self.venues = tuple(venues)
        self.lease_life, self.pulse_every_s, self.pulse_life = lease_life, pulse_every_s, pulse_life
        self.state_path = state_path
        self.poll, self.now, self.log = poll, now, log
        self.code = code_version()
        self.opens: dict[tuple, Open] = {}
        self.closed_streak: dict[tuple, list] = {}   # id -> [count, first_t, RouteHalt]
        self.open_streak: dict[tuple, list] = {}     # id -> [count, first_t]
        self.max_items: dict[str, int] = {}
        self.last_ok: dict[str, int] = {}
        self.last_pulse = 0
        self.tick: dict[str, dict] = {v: {"ok": 0, "fail": 0} for v in self.venues}
        self.last_snap: dict[str, dict] = {}

    # -- state --------------------------------------------------------------------------------

    def load(self) -> None:
        """Rebuild open halts from this reporter's live leases on Arkiv, then merge the local state file
        (which remembers halts whose lease lapsed while the reporter was down)."""
        q = "app = str('%s') AND kind = str('lease') AND $owner = addr(%s)" % (APP, self.w.addr.lower())
        res = self.rpc.query(q, limit=200)
        for row in res.get("data") or []:
            a, p = ak.attrs_of(row), ak.payload_of(row) or {}
            o = Open(a.get("venue", ""), a.get("route", ""), a.get("net", ""), a.get("side", ""), int(a.get("t0", 0)),
                     lease_key=row["key"], expires_at=int(row["expiresAt"], 16),
                     lease_tx=p.get("lease_tx", ""), listed=p.get("listed", 0), max_closed=p.get("max_closed", 0),
                     assets=p.get("assets", []), eta_ms=p.get("eta_ms", 0), first_sha256=p.get("resp_sha256", ""),
                     resumed_from=p.get("resumed_from", ""))
            if o.venue in self.venues:
                self.opens[o.id] = o
        if self.state_path and os.path.exists(self.state_path):
            with open(self.state_path, encoding="utf-8") as f:
                saved = json.load(f)
            for d in saved.get("opens", []):
                o = Open(**d)
                if o.id not in self.opens and o.venue in self.venues:
                    o.gap_since = o.gap_since or saved.get("saved_at", int(self.now()))
                    self.opens[o.id] = o      # lease lapsed while we were down: keep t0, re-lease on next confirm
            self.max_items.update(saved.get("max_items", {}))
            self.last_pulse = saved.get("last_pulse", 0)
        self.log("loaded %d open halts (%s)" % (len(self.opens), ", ".join("%s:%s:%s" % k for k in self.opens)))

    def save(self) -> None:
        if not self.state_path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.state_path)), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"saved_at": int(self.now()), "opens": [asdict(o) for o in self.opens.values()],
                       "max_items": self.max_items, "last_pulse": self.last_pulse}, f)
        os.replace(tmp, self.state_path)

    # -- one poll round -----------------------------------------------------------------------

    def observe(self) -> tuple[list, list]:
        """Poll every venue once. Returns (halts confirmed open now, opens confirmed closed now)."""
        to_open, to_close = [], []
        for venue in self.venues:
            snap = self.poll(venue)
            if snap.ok and snap.items < SHORT_RESPONSE * self.max_items.get(venue, 0):
                snap.ok, snap.error = False, "short response: %d items, max %d" % (snap.items, self.max_items[venue])
            if not snap.ok:
                self.tick[venue]["fail"] += 1
                self.log("%s unreadable: %s" % (venue, snap.error))
                for o in self.opens.values():
                    if o.venue == venue and not o.gap_since:
                        o.gap_since = snap.t
                continue
            self.max_items[venue] = max(self.max_items.get(venue, 0), snap.items)
            self.last_ok[venue] = snap.t
            self.tick[venue]["ok"] += 1
            found, counts, evaluable = vn.halts(snap)
            halted = {(venue, h.route, h.side): h for h in found}
            self.last_snap[venue] = {**counts, "t": snap.t, "resp_sha256": snap.sha256, "resp_bytes": snap.nbytes,
                                     "halted_routes": sorted("%s:%s" % (h.route, h.side) for h in found)}
            for o in [o for o in self.opens.values() if o.venue == venue]:
                if o.gap_since:
                    o.gaps.append([o.gap_since, snap.t])
                    o.gap_since = 0
                h = halted.get(o.id)
                if h:
                    self.open_streak.pop(o.id, None)
                    o.polls += 1
                    o.seen_at = snap.t
                    o.listed, o.max_closed = h.listed, max(o.max_closed, len(h.closed))
                    o.assets, o.eta_ms, o.last_sha256 = h.closed[:MAX_ASSETS_IN_PAYLOAD], h.eta_ms, snap.sha256
                elif (o.route, o.side) in evaluable:
                    s = self.open_streak.setdefault(o.id, [0, snap.t])
                    s[0] += 1
                    if s[0] >= CLOSE_POLLS:
                        to_close.append((o, s[1]))
            for hid, h in halted.items():
                if hid in self.opens:
                    continue
                s = self.closed_streak.setdefault(hid, [0, snap.t, h])
                s[0], s[2] = s[0] + 1, h
                if s[0] >= OPEN_POLLS:
                    to_open.append((h, s[1], snap.sha256))
            for hid in [k for k in self.closed_streak if k[0] == venue and k not in halted]:
                self.closed_streak.pop(hid)
        return to_open, to_close

    # -- writes -------------------------------------------------------------------------------

    def _lease_attrs(self, o: Open) -> list:
        body = {"v": 1, "venue": o.venue, "route": o.route, "net": o.net, "side": o.side, "t0": o.t0,
                "rule": {"min_tradable": vn.MIN_ASSETS, "close_share": vn.CLOSE_SHARE, "open_polls": OPEN_POLLS,
                         "close_polls": CLOSE_POLLS},
                "listed": o.listed, "closed": o.max_closed, "assets": o.assets, "eta_ms": o.eta_ms,
                "src": vn.URLS[o.venue][0], "resp_sha256": o.last_sha256 or o.first_sha256, "code": self.code}
        if o.resumed_from:
            body["resumed_from"] = o.resumed_from
        return [ak.text("app", APP), ak.text("kind", "lease"), ak.text("venue", o.venue), ak.text("route", o.route),
                ak.text("net", o.net), ak.text("side", o.side), ak.u64("t0", o.t0)] + ak.payload(body)

    def _episode_attrs(self, o: Open, t1: int) -> list:
        body = {"v": 1, "venue": o.venue, "route": o.route, "net": o.net, "side": o.side, "t0": o.t0, "t1": t1,
                "dur_s": t1 - o.t0, "rule": {"min_tradable": vn.MIN_ASSETS, "close_share": vn.CLOSE_SHARE,
                                             "open_polls": OPEN_POLLS, "close_polls": CLOSE_POLLS},
                "listed": o.listed, "max_closed": o.max_closed, "assets": o.assets, "eta_ms": o.eta_ms,
                "polls": o.polls, "gaps": o.gaps, "lease_key": o.lease_key, "lease_tx": o.lease_tx,
                "first_sha256": o.first_sha256, "last_sha256": o.last_sha256, "src": vn.URLS[o.venue][0],
                "code": self.code}
        return [ak.text("app", APP), ak.text("kind", "episode"), ak.text("venue", o.venue), ak.text("route", o.route),
                ak.text("net", o.net), ak.text("side", o.side), ak.u64("t0", o.t0), ak.u64("t1", t1),
                ak.u64("dur_s", max(0, t1 - o.t0))] + ak.payload(body)

    def _send(self, ops: list, what: str):
        try:
            rc = self.w.send(ops)
        except ak.ArkivError as e:
            self.log("%s failed: %s" % (what, e))
            return None
        self.log("%s: tx %s block %d gas %d" % (what, rc.tx, rc.block, rc.gas_used))
        return rc

    def write_opens(self, to_open: list) -> None:
        """New halts, and lapsed halts that are still confirmed (re-leased with their original t0)."""
        batch = []
        for h, t0, sha in to_open:
            batch.append(Open(h.venue, h.route, h.net, h.side, t0, listed=h.listed, max_closed=len(h.closed),
                              assets=h.closed[:MAX_ASSETS_IN_PAYLOAD], eta_ms=h.eta_ms, first_sha256=sha,
                              last_sha256=sha, polls=OPEN_POLLS, seen_at=int(self.now())))
        head = self.rpc.block_number()
        for o in self.opens.values():
            if not o.lease_key or o.expires_at <= head + 2:
                if self.now() - o.seen_at < 600 and o.id not in self.open_streak:
                    o.resumed_from = o.lease_key or o.resumed_from
                    batch.append(o)
        if not batch:
            return
        nonce = self.rpc.entity_nonce(self.w.addr)
        ops, keys = [], []
        for i, o in enumerate(batch):
            op, salt = ak.op_create(self._lease_attrs(o), min_lifetime=self.lease_life, flags=ak.READONLY)
            ops.append(op)
            keys.append(ak.entity_key(self.w.addr, nonce + i, salt))
        rc = self._send(ops, "open %d lease(s)" % len(ops))
        if not rc:
            return
        exp = self._expiries(rc)
        for o, k in zip(batch, keys):
            if k not in rc.created:
                self.log("lease key mismatch for %s:%s:%s" % o.id)
                continue
            o.lease_key, o.lease_tx, o.expires_at = k, rc.tx, exp.get(k, rc.block + self.lease_life)
            self.opens[o.id] = o
            self.closed_streak.pop(o.id, None)

    def write_closes(self, to_close: list) -> None:
        if not to_close:
            return
        head = self.rpc.block_number()
        nonce = self.rpc.entity_nonce(self.w.addr)
        ops, done = [], []
        for o, t1 in to_close:
            op, salt = ak.op_create(self._episode_attrs(o, t1), min_lifetime=EPISODE_LIFE,
                                    flags=ak.READONLY | ak.PERMISSIONLESS_EXTENSION)
            key = ak.entity_key(self.w.addr, nonce + len(done), salt)
            ops += [op, ak.op_transfer(key, ak.BURN)]
            if o.lease_key and o.expires_at > head + 5:
                ops.append(ak.op_delete(o.lease_key))
            done.append((o, key, t1))
        rc = self._send(ops, "close %d halt(s)" % len(done))
        if not rc:
            return
        for o, key, t1 in done:
            self.log("episode %s %s:%s:%s %ds" % (key, o.venue, o.route, o.side, t1 - o.t0))
            self.opens.pop(o.id, None)
            self.open_streak.pop(o.id, None)

    def write_heartbeat(self) -> None:
        head = self.rpc.block_number()
        due = [o for o in self.opens.values()
               if o.lease_key and head + 5 < o.expires_at < head + self.lease_life * 2 // 3
               and self.now() - self.last_ok.get(o.venue, 0) < STALE_S]
        if not due:
            return
        rc = self._send([ak.op_extend(o.lease_key, self.lease_life) for o in due], "renew %d lease(s)" % len(due))
        if rc:
            exp = self._expiries(rc)
            for o in due:
                o.expires_at = exp.get(o.lease_key, head + self.lease_life)

    def write_pulses(self, force: bool = False) -> None:
        if not force and self.now() - self.last_pulse < self.pulse_every_s:
            return
        ops = []
        for v in self.venues:
            snap = self.last_snap.get(v, {})
            body = {"v": 1, "venue": v, "t": int(self.now()), "polls_ok": self.tick[v]["ok"],
                    "polls_fail": self.tick[v]["fail"], "src": vn.URLS[v][0], "code": self.code, **snap}
            op, _ = ak.op_create([ak.text("app", APP), ak.text("kind", "pulse"), ak.text("venue", v)]
                                 + ak.payload(body), min_lifetime=self.pulse_life, flags=ak.READONLY)
            ops.append(op)
        if self._send(ops, "pulse x%d" % len(ops)):
            self.last_pulse = int(self.now())
            self.tick = {v: {"ok": 0, "fail": 0} for v in self.venues}

    @staticmethod
    def _expiries(rc: ak.Receipt) -> dict:
        out = {}
        for lg in rc.logs:
            t0 = lg["topics"][0]
            if t0 in (ak.TOPIC_CREATED, ak.TOPIC_EXTENDED):
                out[lg["topics"][1]] = int(lg["data"][2:66], 16)
        return out

    def cycle(self) -> None:
        to_open, to_close = self.observe()
        self.write_closes(to_close)
        self.write_opens(to_open)
        self.write_heartbeat()
        self.write_pulses()
        self.save()
