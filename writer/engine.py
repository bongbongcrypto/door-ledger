"""The reporter: turns venue polls into Arkiv entities.

Entity kinds (all carry app='doorledger'):
  lease    an open halt as this reporter sees it now. Short life, renewed only while the halt is still
           seen; if the reporter dies or loses sight of it, the lease lapses and the halt leaves
           "Open now" by itself.
  episode  a closed halt (the ledger row). Read-only + extendable by anyone, 180 days, and moved to
           0x...dEaD in the same transaction, so its creator can no longer delete or edit it.
  pulse    one per venue per tick: proof that the reporter is alive and could read the venue.

Confirmation: a halt opens after OPEN_POLLS consecutive halted polls (shut share >= CLOSE_SHARE) and
closes after CLOSE_POLLS consecutive judged polls at or below the open line (shut share <= OPEN_LINE,
or every originally shut asset open again). A poll between the two lines changes nothing, and a poll
that cannot judge a route (venue down, short response, route missing) changes nothing.

Write safety, in order of importance:
  * one transaction in flight per wallet; a signed transaction is recorded before it is broadcast and
    every write starts by settling it (by receipt, then by whether the node still knows it);
  * every write re-checks its inputs against the settled state, so a halt that a just-settled
    transaction already opened or closed is never written again;
  * an episode never leaves without its transfer to 0x...dEaD: only renewals and lease deletions are
    ever dropped from a batch, and only when they name a lease of ours that is already gone;
  * created keys are read from the receipt in op order, never trusted from a prediction.
"""
from __future__ import annotations

import dataclasses
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
SHORT_RESPONSE = 0.9          # a response smaller than this share of the venue's usual size is ignored
SHORT_TRADABLE = 0.5          # same guard for the tradable list (a second endpoint at KuCoin and Bithumb)
STALE_S = 30 * 60             # stop renewing a lease when its halt has not been seen for this long
RELEASE_S = 600               # a lapsed halt is re-leased only if it was seen this recently
PULSE_GAP_BLOCKS = 1700       # once mode: skip the pulse if this reporter pulsed within ~57 min
PENDING_GIVE_UP_S = 600       # an unmined tx the node does not know is written off after this
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
    assets: list = field(default_factory=list)      # every shut asset at the last confirmation (full list)
    eta_unix_ms: int = 0
    polls: int = 0
    gaps: list = field(default_factory=list)
    first_sha256: str = ""
    last_sha256: str = ""
    resumed_from: str = ""
    gap_since: int = 0
    seen_at: int = 0            # last poll (watcher clock) that confirmed the halt
    extra_leases: list = field(default_factory=list)  # duplicate live leases for this halt, deleted on close

    @property
    def id(self) -> tuple:
        return (self.venue, self.route, self.side)


def make_open(d: dict) -> Open:
    names = {f.name for f in dataclasses.fields(Open)}
    return Open(**{k: v for k, v in d.items() if k in names})


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
                 pulse_life: int = 4500, state_path: str | None = None, sampled: bool = False, poll=vn.poll,
                 now=time.time, log=print):
        self.w, self.rpc = writer, writer.rpc
        self.venues = tuple(venues)
        self.lease_life, self.pulse_every_s, self.pulse_life = lease_life, pulse_every_s, pulse_life
        self.state_path = state_path
        self.sampled = sampled            # once mode: polls are samples, coverage gaps are not tracked
        self.poll, self.now, self.log = poll, now, log
        self.code = code_version()
        self.opens: dict[tuple, Open] = {}
        self.closed_streak: dict[tuple, list] = {}   # id -> [count, first_t]
        self.open_streak: dict[tuple, list] = {}     # id -> [count, first_t]
        self.size_hist: dict[str, dict] = {}         # venue -> {hour: [items, tradable]}
        self.last_ok: dict[str, int] = {}
        self.last_pulse = 0
        self.last_pulse_block = 0
        self.pending: dict | None = None
        self.inflight_nonce: int | None = None       # a tx from an earlier process that is still unmined
        self.tick: dict[str, dict] = {v: {"ok": 0, "fail": 0} for v in self.venues}
        self.last_snap: dict[str, dict] = {}

    # -- state --------------------------------------------------------------------------------

    def load(self) -> None:
        """Rebuild open halts from this reporter's live leases on Arkiv, merged with the local state file
        (which keeps coverage gaps, poll counts and halts whose lease lapsed while the reporter was down)."""
        saved = {}
        if self.state_path and os.path.exists(self.state_path):
            with open(self.state_path, encoding="utf-8") as f:
                saved = json.load(f)
        saved_list = [make_open(d) for d in saved.get("opens", [])]
        saved_at = int(saved.get("saved_at", 0))
        q = "app = str('%s') AND (kind = str('lease') OR kind = str('pulse')) AND $owner = addr(%s)" % (
            APP, self.w.addr.lower())
        rows, cursor, at = [], None, None
        for _ in range(20):
            res = self.rpc.query(q, at_block=at, limit=200, cursor=cursor)
            rows += res.get("data") or []
            cursor = res.get("cursor")
            if not cursor:
                break
            at = int(res["blockNumber"], 16)
        chain: dict[tuple, Open] = {}
        for row in rows:
            a, p = ak.attrs_of(row), ak.payload_of(row) or {}
            if a.get("kind") == "pulse":
                self.last_pulse_block = max(self.last_pulse_block, int(row["createdAt"], 16))
                continue
            o = Open(a.get("venue", ""), a.get("route", ""), a.get("net", ""), a.get("side", ""), int(a.get("t0", 0)),
                     lease_key=row["key"].lower(), expires_at=int(row["expiresAt"], 16), listed=p.get("listed", 0),
                     max_closed=p.get("closed", 0), assets=list(p.get("assets", [])),
                     eta_unix_ms=p.get("eta_unix_ms", 0),
                     first_sha256=p.get("first_sha256") or p.get("resp_sha256", ""),
                     last_sha256=p.get("resp_sha256", ""), resumed_from=p.get("resumed_from", ""))
            o.lease_tx = ""
            if o.venue not in self.venues:
                continue
            o._created = int(row["createdAt"], 16)  # type: ignore[attr-defined]
            have = chain.get(o.id)
            if have is None:
                chain[o.id] = o
            else:                                   # two live leases for one halt: keep the older claim
                keep, extra = (have, o) if have.t0 <= o.t0 else (o, have)
                keep.extra_leases = sorted(set(keep.extra_leases + [extra.lease_key] + extra.extra_leases))
                chain[o.id] = keep
        for o in chain.values():
            old = self._match_saved(saved_list, o)
            if old:
                saved_list.remove(old)
                o.lease_tx, o.polls, o.gaps, o.seen_at = old.lease_tx, old.polls, old.gaps, old.seen_at
                o.first_sha256 = old.first_sha256 or o.first_sha256
                o.max_closed = max(o.max_closed, old.max_closed)
                if len(old.assets) > len(o.assets):
                    o.assets = old.assets
                o.extra_leases = sorted(set(o.extra_leases + old.extra_leases) - {o.lease_key})
            if not o.lease_tx:
                o.lease_tx = self._creation_tx(o.lease_key, o._created)  # type: ignore[attr-defined]
            if not self.sampled and saved_at:
                o.gap_since = (old.gap_since if old else 0) or saved_at   # down since the last save
            del o._created  # type: ignore[attr-defined]
            self.opens[o.id] = o
        for o in saved_list:                        # lease lapsed while we were down: keep t0, re-lease on sight
            if o.venue in self.venues and o.id not in self.opens:
                o.gap_since = o.gap_since or saved_at
                self.opens[o.id] = o
        self.size_hist = saved.get("size_hist", {})
        self.last_pulse = saved.get("last_pulse", 0)
        self.pending = saved.get("pending")
        if not self.pending:
            try:
                latest, pend = self.w.nonce_latest(), self.w.nonce_pending()
                if pend > latest:
                    self.inflight_nonce = pend - 1
                    self.log("an earlier transaction (nonce %d) is still unmined; holding writes" % self.inflight_nonce)
            except (ak.ArkivError, AttributeError):
                pass
        self.log("loaded %d open halts (%s)%s" % (len(self.opens), ", ".join("%s:%s:%s" % k for k in self.opens),
                                                   "; resolving tx %s" % self.pending["tx"] if self.pending else ""))

    @staticmethod
    def _match_saved(saved: list, o: Open):
        for test in (lambda s: s.lease_key and s.lease_key == o.lease_key,
                     lambda s: o.resumed_from and s.lease_key == o.resumed_from,
                     lambda s: s.id == o.id):
            for s in saved:
                if test(s):
                    return s
        return None

    def _creation_tx(self, key: str, block: int) -> str:
        try:
            logs = self.rpc.call("eth_getLogs", [{"address": ak.REGISTRY, "fromBlock": hex(block), "toBlock": hex(block),
                                                  "topics": [ak.TOPIC_CREATED, key]}])
            return logs[0]["transactionHash"] if logs else ""
        except ak.ArkivError:
            return ""

    def save(self) -> None:
        if not self.state_path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.state_path)), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"saved_at": int(self.now()), "opens": [asdict(o) for o in self.opens.values()],
                       "size_hist": self.size_hist, "last_pulse": self.last_pulse, "pending": self.pending}, f)
        os.replace(tmp, self.state_path)

    # -- response-size guard (24 h window, robust to one oversized hour) ----------------------

    def _size_ok(self, snap: vn.Snapshot) -> str:
        hist = self.size_hist.setdefault(snap.venue, {})
        now_h = snap.t // 3600
        for h in [h for h in hist if int(h) < now_h - 24]:
            hist.pop(h)
        if hist:
            items = sorted(v[0] for v in hist.values())
            trad = sorted(v[1] for v in hist.values())
            ref_items, ref_trad = items[len(items) // 2], trad[len(trad) // 2]
            if snap.items < SHORT_RESPONSE * ref_items:
                return "short response: %d items, usual %d" % (snap.items, ref_items)
            if len(snap.tradable) < SHORT_TRADABLE * ref_trad:
                return "short tradable list: %d, usual %d" % (len(snap.tradable), ref_trad)
        cur = hist.get(str(now_h), [0, 0])
        hist[str(now_h)] = [max(cur[0], snap.items), max(cur[1], len(snap.tradable))]
        return ""

    # -- one poll round -----------------------------------------------------------------------

    def observe(self) -> tuple[list, list]:
        """Poll every venue once. Returns (halts confirmed open now, opens confirmed closed now)."""
        to_open, to_close = [], []
        for venue in self.venues:
            snap = self.poll(venue)
            if snap.ok:
                why = self._size_ok(snap)
                if why:
                    snap.ok, snap.error = False, why
            if not snap.ok:
                self.tick[venue]["fail"] += 1
                self.log("%s unreadable: %s" % (venue, snap.error))
                for o in self.opens.values():
                    if o.venue == venue and not o.gap_since and not self.sampled:
                        o.gap_since = snap.t
                continue
            self.last_ok[venue] = snap.t
            self.tick[venue]["ok"] += 1
            found, counts, judged = vn.halts(snap)
            halted = {(venue, h.route, h.side): h for h in found}
            self.last_snap[venue] = {**counts, "resp_sha256": snap.sha256, "resp_bytes": snap.nbytes,
                                     "halted_routes": sorted("%s:%s" % (h.route, h.side) for h in found)}
            for o in [o for o in self.opens.values() if o.venue == venue]:
                j = judged.get((o.route, o.side))
                if o.gap_since and j:
                    o.gaps.append([o.gap_since, snap.t])
                    o.gap_since = 0
                h = halted.get(o.id)
                if h:
                    self.open_streak.pop(o.id, None)
                    o.polls += 1
                    o.seen_at = snap.t
                    o.listed, o.max_closed = h.listed, max(o.max_closed, len(h.closed))
                    o.assets, o.eta_unix_ms, o.last_sha256 = list(h.closed), h.eta_ms, snap.sha256
                    continue
                if not j:                                   # cannot judge: unknown, change nothing
                    if not o.gap_since and not self.sampled:
                        o.gap_since = snap.t
                    continue
                shut, known, real_shut = j
                still = set(o.assets) & real_shut
                reopened = shut <= vn.OPEN_LINE * known or (o.assets and not still)
                if reopened:
                    s = self.open_streak.setdefault(o.id, [0, snap.t])
                    s[0] += 1
                    if s[0] >= CLOSE_POLLS:
                        to_close.append((o, s[1]))
                else:
                    self.open_streak.pop(o.id, None)          # between the lines: not a clean reopen
                    if o.assets and len(still) >= vn.CLOSE_SHARE * len(o.assets):
                        o.seen_at = snap.t                    # the originally shut assets are still shut
            for hid, h in halted.items():
                if hid in self.opens:
                    continue
                s = self.closed_streak.setdefault(hid, [0, snap.t])
                s[0] += 1
                if s[0] >= OPEN_POLLS:
                    to_open.append((h, s[1], snap.sha256, snap.t))
            for hid in [k for k in self.closed_streak
                        if k[0] == venue and k not in halted and (k[1], k[2]) in judged]:
                self.closed_streak.pop(hid)                   # judged and not halted: the streak is broken
        return to_open, to_close

    # -- payloads -----------------------------------------------------------------------------

    def _rule(self) -> dict:
        return {"min_tradable": vn.MIN_ASSETS, "close_share": vn.CLOSE_SHARE, "open_line": vn.OPEN_LINE,
                "open_polls": OPEN_POLLS, "close_polls": CLOSE_POLLS}

    def _lease_attrs(self, o: Open) -> list:
        body = {"v": 1, "venue": o.venue, "route": o.route, "net": o.net, "side": o.side, "t0": o.t0,
                "rule": self._rule(), "listed": o.listed, "closed": o.max_closed,
                "assets": o.assets[:MAX_ASSETS_IN_PAYLOAD], "eta_unix_ms": o.eta_unix_ms, "src": vn.URLS[o.venue][0],
                "first_sha256": o.first_sha256, "resp_sha256": o.last_sha256 or o.first_sha256, "code": self.code,
                "coverage": "sampled" if self.sampled else "continuous"}
        if o.resumed_from:
            body["resumed_from"] = o.resumed_from
        return [ak.text("app", APP), ak.text("kind", "lease"), ak.text("venue", o.venue), ak.text("route", o.route),
                ak.text("net", o.net), ak.text("side", o.side), ak.u64("t0", o.t0)] + ak.payload(body)

    def _episode_attrs(self, o: Open, t1: int) -> list:
        body = {"v": 1, "venue": o.venue, "route": o.route, "net": o.net, "side": o.side, "t0": o.t0, "t1": t1,
                "dur_s": t1 - o.t0, "rule": self._rule(), "listed": o.listed, "max_closed": o.max_closed,
                "assets": o.assets[:MAX_ASSETS_IN_PAYLOAD], "eta_unix_ms": o.eta_unix_ms, "lease_key": o.lease_key,
                "lease_tx": o.lease_tx, "first_sha256": o.first_sha256, "last_sha256": o.last_sha256,
                "src": vn.URLS[o.venue][0], "code": self.code, "coverage": "sampled" if self.sampled else "continuous"}
        if not self.sampled:
            body["polls"], body["gaps"] = o.polls, o.gaps
        return [ak.text("app", APP), ak.text("kind", "episode"), ak.text("venue", o.venue), ak.text("route", o.route),
                ak.text("net", o.net), ak.text("side", o.side), ak.u64("t0", o.t0), ak.u64("t1", t1),
                ak.u64("dur_s", max(0, t1 - o.t0))] + ak.payload(body)

    # -- settling ----------------------------------------------------------------------------

    def resolve_pending(self) -> bool:
        """Settle a transaction whose outcome was unknown. True when the wallet is clear to send."""
        if self.inflight_nonce is not None and not self.pending:
            try:
                if self.w.nonce_latest() > self.inflight_nonce:
                    self.inflight_nonce = None
                else:
                    return False
            except ak.ArkivError:
                return False
        p = self.pending
        if not p:
            return True
        try:
            rc = self.w.receipt(p["tx"])
        except ak.ReceiptUnknown:
            self.log("cannot read the receipt of %s yet; holding writes" % p["tx"])
            return False
        except ak.ArkivError as e:                       # mined but reverted: nothing changed on chain
            self.log("pending %s %s: %s" % (p["kind"], p["tx"], e))
            self.pending = None
            self.save()
            return True
        if rc:
            self.log("pending %s %s landed in block %d" % (p["kind"], p["tx"], rc.block))
            self.pending = None
            self._apply(p["kind"], p["data"], rc)
            self.save()
            return True
        if self.w.known(p["tx"]):
            self.log("tx %s still pending; holding writes" % p["tx"])
            return False
        try:
            moved = self.w.nonce_latest() > p["nonce"]
        except ak.ArkivError:
            return False
        if not moved and p.get("raw") and self.now() - p["sent_at"] < PENDING_GIVE_UP_S:
            try:                                         # never reached the node (or it forgot): resend as is
                self.rpc.call("eth_sendRawTransaction", [p["raw"]])
                self.log("rebroadcast %s" % p["tx"])
            except ak.ArkivError as e:
                self.log("rebroadcast of %s failed: %s" % (p["tx"], e))
            return False
        if moved or self.now() - p["sent_at"] > PENDING_GIVE_UP_S:
            self.log("pending %s %s was dropped; its writes will be rebuilt" % (p["kind"], p["tx"]))
            self.pending = None
            self.save()
            return True
        return False

    def _send(self, ops: list, kind: str, data: dict, what: str):
        """Send one batch. Only a renewal or lease deletion that names a lease of ours that is already
        gone is ever dropped (then the rest is retried); anything else fails the batch for this cycle."""
        for _ in range(4):
            if not ops:
                return None
            if not self.resolve_pending():
                return None

            def remember(h, nonce):
                self.pending = {"tx": h, "nonce": nonce, "raw": getattr(self.w, "last_raw", None), "kind": kind,
                                "data": data, "sent_at": int(self.now())}
                self.save()
            try:
                rc = self.w.send(ops, on_signed=remember)
            except ak.ArkivPending as e:
                self.log("%s: %s" % (what, e))
                return None
            except ak.ArkivError as e:
                self.pending = None
                dead = ak.dead_key(e)
                droppable = [op for op in ops if op.ref == dead and op.tag in (ak.OP_EXTEND, ak.OP_DELETE)]
                if dead and droppable and self._is_our_lease(dead):
                    self.log("%s: lease %s is already gone; dropping it and retrying" % (what, dead))
                    self._lease_gone(dead)
                    ops = [op for op in ops if op not in droppable]
                    data = {**data, "keys": [k for k in data.get("keys", []) if k != dead]}
                    continue
                self.log("%s failed: %s" % (what, e))
                return None
            self.pending = None
            self.log("%s: tx %s block %d gas %d" % (what, rc.tx, rc.block, rc.gas_used))
            self._apply(kind, data, rc)
            self.save()
            return rc
        return None

    def _is_our_lease(self, key: str) -> bool:
        return any(o.lease_key == key or key in o.extra_leases for o in self.opens.values())

    def _lease_gone(self, key: str) -> None:
        for o in self.opens.values():
            if o.lease_key == key:
                o.expires_at = 0                    # write_opens re-leases it if the halt is still seen
            if key in o.extra_leases:
                o.extra_leases.remove(key)

    def _apply(self, kind: str, data: dict, rc: ak.Receipt) -> None:
        exp = self._expiries(rc)
        if kind == "open":
            batch = data["opens"]
            if len(rc.created) != len(batch):
                self.log("open receipt %s created %d entities for %d leases" % (rc.tx, len(rc.created), len(batch)))
            for d, k in zip(batch, rc.created):        # an open batch holds only creates, in order
                k = k.lower()
                o = self.opens.get(make_open(d).id)
                if o is None:
                    o = make_open(d)
                    self.opens[o.id] = o
                elif o.lease_key and o.lease_key != d.get("resumed_from") and o.expires_at > rc.block:
                    o.extra_leases = sorted(set(o.extra_leases + [o.lease_key]))   # should not happen; never orphan
                o.lease_key, o.lease_tx, o.expires_at = k, rc.tx, exp.get(k, rc.block + self.lease_life)
                o.resumed_from = d.get("resumed_from", "")
                o.seen_at = max(o.seen_at, d.get("seen_at", 0))
                self.closed_streak.pop(o.id, None)
        elif kind == "close":
            for oid in data["ids"]:
                self.opens.pop(tuple(oid), None)
                self.open_streak.pop(tuple(oid), None)
        elif kind == "renew":
            for o in self.opens.values():
                if o.lease_key in exp:
                    o.expires_at = exp[o.lease_key]
        elif kind == "pulse":
            self.last_pulse = int(self.now())
            self.last_pulse_block = rc.block
            self.tick = {v: {"ok": 0, "fail": 0} for v in self.venues}

    @staticmethod
    def _expiries(rc: ak.Receipt) -> dict:
        out = {}
        for lg in rc.logs:
            if lg["topics"][0] in (ak.TOPIC_CREATED, ak.TOPIC_EXTENDED):
                out[lg["topics"][1].lower()] = int(lg["data"][2:66], 16)
        return out

    # -- writes (each starts from settled state) ---------------------------------------------

    def write_opens(self, to_open: list) -> None:
        """New halts, and lapsed halts that are still seen (re-leased with their original t0)."""
        if not self.resolve_pending():
            return
        batch = []
        for h, t0, sha, seen in to_open:
            if (h.venue, h.route, h.side) in self.opens:      # a just-settled tx already opened it
                continue
            batch.append(Open(h.venue, h.route, h.net, h.side, t0, listed=h.listed, max_closed=len(h.closed),
                              assets=list(h.closed), eta_unix_ms=h.eta_ms, first_sha256=sha, last_sha256=sha,
                              polls=OPEN_POLLS, seen_at=seen))
        head = self.rpc.block_number()
        for o in self.opens.values():
            if (not o.lease_key or o.expires_at <= head + 2) and self.now() - o.seen_at < RELEASE_S \
                    and o.id not in self.open_streak:
                batch.append(dataclasses.replace(o, resumed_from=o.lease_key or o.resumed_from, lease_key=""))
        if not batch:
            return
        ops = [ak.op_create(self._lease_attrs(o), min_lifetime=self.lease_life, flags=ak.READONLY)[0] for o in batch]
        self._send(ops, "open", {"opens": [asdict(o) for o in batch]}, "open %d lease(s)" % len(ops))

    def write_closes(self, to_close: list) -> None:
        if not to_close or not self.resolve_pending():
            return
        to_close = [(o, t1) for o, t1 in to_close if self.opens.get(o.id) is o]   # drop what is already closed
        if not to_close:
            return
        head = self.rpc.block_number()
        nonce = self.rpc.entity_nonce(self.w.addr)
        ops, ids = [], []
        for i, (o, t1) in enumerate(to_close):
            op, salt = ak.op_create(self._episode_attrs(o, t1), min_lifetime=EPISODE_LIFE,
                                    flags=ak.READONLY | ak.PERMISSIONLESS_EXTENSION)
            key = ak.entity_key(self.w.addr, nonce + i, salt)    # transfers and deletes use no entity nonce
            ops += [op, ak.op_transfer(key, ak.BURN)]
            for lease in [o.lease_key] + o.extra_leases:
                if lease and (lease != o.lease_key or o.expires_at > head + 5):
                    ops.append(ak.op_delete(lease))
            ids.append(list(o.id))
            self.log("closing %s:%s:%s after %ds as %s" % (o.venue, o.route, o.side, t1 - o.t0, key))
        self._send(ops, "close", {"ids": ids, "keys": [op.ref for op in ops if op.tag == ak.OP_DELETE]},
                   "close %d halt(s)" % len(ids))

    def write_heartbeat(self) -> None:
        if not self.resolve_pending():
            return
        head = self.rpc.block_number()
        due = [o for o in self.opens.values()
               if o.lease_key and head + 5 < o.expires_at < head + self.lease_life * 2 // 3
               and self.now() - o.seen_at < STALE_S]
        if due:
            self._send([ak.op_extend(o.lease_key, self.lease_life) for o in due], "renew",
                       {"keys": [o.lease_key for o in due]}, "renew %d lease(s)" % len(due))

    def write_pulses(self, force: bool = False) -> None:
        if force:
            if self.last_pulse_block and self.rpc.block_number() - self.last_pulse_block < PULSE_GAP_BLOCKS:
                return
        elif self.now() - self.last_pulse < self.pulse_every_s:
            return
        if not self.resolve_pending():
            return
        ops = []
        for v in self.venues:
            body = {"v": 1, "venue": v, "t": int(self.now()), "polls_ok": self.tick[v]["ok"],
                    "polls_fail": self.tick[v]["fail"], "src": vn.URLS[v][0], "code": self.code,
                    "coverage": "sampled" if self.sampled else "continuous", **self.last_snap.get(v, {})}
            op, _ = ak.op_create([ak.text("app", APP), ak.text("kind", "pulse"), ak.text("venue", v)]
                                 + ak.payload(body), min_lifetime=self.pulse_life, flags=ak.READONLY)
            ops.append(op)
        self._send(ops, "pulse", {}, "pulse x%d" % len(ops))

    def cycle(self) -> None:
        to_open, to_close = self.observe()
        if not self.resolve_pending():
            self.save()
            return
        self.write_closes(to_close)
        self.write_opens(to_open)
        self.write_heartbeat()
        self.write_pulses()
        self.save()
