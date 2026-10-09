"""Venue parsers and the network halt rule (writer/venues.py).

The fixtures in tests/fixtures/ are public exchange responses recorded in October 2026, trimmed to
the coins these cases need:
  - binance_capital_config.json: 15 coins; coin and network objects keep only a subset of fields.
  - gate_currencies.json: 33 whole currency objects (the CRO and ABS routes, the SUI/SUINEW pair,
    a few delisted coins).
  - kucoin_currencies.json + kucoin_symbols.json: 13 whole currency objects (the ton/ton2 pair) and
    the first trading pair of each tradable one.
  - bithumb_multichain.json + bithumb_markets.json: 24 network rows and their markets (markets keep
    market, english_name and market_warning).
64-hex object ids (Sui) are shortened to their first 12 hex digits, the same way on every route, so
contract equality between routes is unchanged.
"""
from __future__ import annotations

import copy
import gzip
import hashlib
import http.client
import json
import os
import unittest
import urllib.error
import zlib
from unittest import mock

from writer import venues as vn

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name: str) -> bytes:
    with open(os.path.join(FIX, name), "rb") as f:
        return f.read()


def snapshot(venue: str, rows, tradable, items: int | None = None) -> vn.Snapshot:
    return vn.Snapshot(venue, 1_800_000_000, True, list(rows), set(tradable),
                       len(rows) if items is None else items)


def gate_snapshot(body: bytes | None = None) -> vn.Snapshot:
    rows, tradable, items = vn.parse_gate(body if body is not None else fixture("gate_currencies.json"))
    return snapshot("gate", rows, tradable, items)


def kucoin_snapshot() -> vn.Snapshot:
    rows, tradable, items = vn.parse_kucoin(fixture("kucoin_currencies.json"), fixture("kucoin_symbols.json"))
    return snapshot("kucoin", rows, tradable, items)


def pairs(found) -> set:
    return {(h.route, h.side) for h in found}


def row(rows, asset: str, route: str) -> vn.Row:
    hits = [r for r in rows if r.asset == asset and r.route == route]
    if len(hits) != 1:
        raise AssertionError("expected one row for %s on %s, got %d" % (asset, route, len(hits)))
    return hits[0]


class ParserTests(unittest.TestCase):
    def test_binance(self):
        rows, tradable, items = vn.parse_binance(fixture("binance_capital_config.json"))
        self.assertEqual(items, 15)
        self.assertEqual(len(rows), 46)
        # fiat (isLegalMoney) is skipped entirely; coins with trading=false keep their rows but are not tradable
        self.assertNotIn("EUR", {r.asset for r in rows})
        self.assertNotIn("EUR", tradable)
        for coin in ("HNT", "STG", "STGOLD"):
            self.assertNotIn(coin, tradable)
            self.assertIn(coin, {r.asset for r in rows})
        self.assertEqual(tradable, {"AVAX", "BNB", "BTC", "ETH", "GMX", "JOE", "LINK", "QI", "SEI", "SOL", "UNI"})
        # route = Binance `network`; switches and the recovery estimate come through as recorded
        sei = row(rows, "SEI", "SEIEVM")
        self.assertIs(sei.deposit_open, False)
        self.assertIs(sei.withdraw_open, False)
        self.assertEqual(sei.eta_ms, 1791519900000)
        btc = row(rows, "BTC", "BTC")
        self.assertIs(btc.deposit_open, True)
        self.assertIs(btc.withdraw_open, True)
        self.assertEqual(btc.eta_ms, 0)
        self.assertTrue(all(r.contract == r.contract.lower() for r in rows))
        self.assertTrue(row(rows, "LINK", "ETH").contract.startswith("0x"))

    def test_gate(self):
        rows, tradable, items = vn.parse_gate(fixture("gate_currencies.json"))
        self.assertEqual(items, 33)
        self.assertEqual(len(rows), 70)
        # delisted or trade-disabled currencies are not tradable
        for cur in ("BBC", "FER", "FUL", "MTD", "SUIA", "SUIP"):
            self.assertNotIn(cur, tradable)
        for cur in ("CRO", "CAW", "ETH", "SUI", "BLUAI"):
            self.assertIn(cur, tradable)
        # route = chains[].name; deposit_disabled/withdraw_disabled are inverted into *_open
        caw = row(rows, "CAW", "CRO")
        self.assertIs(caw.deposit_open, False)
        self.assertIs(caw.withdraw_open, True)
        hippo_old, hippo_new = row(rows, "HIPPO", "SUI"), row(rows, "HIPPO", "SUINEW")
        self.assertIs(hippo_old.deposit_open, True)
        self.assertIs(hippo_new.deposit_open, False)
        self.assertEqual(hippo_old.contract, hippo_new.contract)
        self.assertTrue(all(r.contract == r.contract.lower() for r in rows))

    def test_gate_skips_placeholder_chains(self):
        body = json.dumps([{"currency": "ABC", "delisted": False, "trade_disabled": False, "chains": [
            {"name": "ITSNOTACHAIN", "deposit_disabled": True, "withdraw_disabled": True},
            {"name": "", "deposit_disabled": True, "withdraw_disabled": True},
            {"name": "ETH", "addr": "0xAbC", "deposit_disabled": False, "withdraw_disabled": False}]}]).encode()
        rows, tradable, items = vn.parse_gate(body)
        self.assertEqual([(r.route, r.contract) for r in rows], [("ETH", "0xabc")])
        self.assertEqual((tradable, items), ({"ABC"}, 1))

    def test_kucoin(self):
        rows, tradable, items = vn.parse_kucoin(fixture("kucoin_currencies.json"), fixture("kucoin_symbols.json"))
        self.assertEqual(items, 13)
        # tradable = base currencies of trading-enabled symbols; REDO, WAT and CSP have no symbol
        self.assertEqual(tradable, {"BTC", "CATI", "DOGS", "ETH", "GRAM", "HMSTR", "MAJOR", "NOT", "TAC", "USDT"})
        # route = chainId: both TON routes are displayed as "TON" but stay apart
        not_routes = sorted(r.route for r in rows if r.asset == "NOT")
        self.assertEqual(not_routes, ["ton", "ton2"])
        self.assertIs(row(rows, "NOT", "ton").withdraw_open, True)
        self.assertIs(row(rows, "NOT", "ton2").withdraw_open, False)
        self.assertIs(row(rows, "NOT", "ton2").deposit_open, True)
        self.assertEqual(row(rows, "NOT", "ton").contract, row(rows, "NOT", "ton2").contract)

    def test_kucoin_disabled_symbol_is_not_tradable(self):
        cur = json.dumps({"code": "200000", "data": [{"currency": "AAA", "chains": []}]}).encode()
        sym = json.dumps({"code": "200000", "data": [{"symbol": "AAA-USDT", "baseCurrency": "AAA", "enableTrading": False},
                                                     {"symbol": "BBB-USDT", "baseCurrency": "BBB", "enableTrading": True}]}).encode()
        _, tradable, _ = vn.parse_kucoin(cur, sym)
        self.assertEqual(tradable, {"BBB"})

    def test_bithumb(self):
        rows, tradable, items = vn.parse_bithumb(fixture("bithumb_multichain.json"), fixture("bithumb_markets.json"))
        self.assertEqual(items, 24)
        self.assertEqual(len(rows), 24)
        icx, zil = row(rows, "ICX", "ICX"), row(rows, "ZIL", "ZIL")
        self.assertEqual((icx.deposit_open, icx.withdraw_open), (False, True))
        self.assertEqual((zil.deposit_open, zil.withdraw_open), (False, False))
        self.assertEqual(sorted(r.route for r in rows if r.asset == "ETH"), ["ARB_ETH", "ETH", "OP_ETH"])
        # tradable = quote-stripped markets; the EOS-family tokens have no market
        for cur in ("EOSDAC", "MEETONE", "HORUS"):
            self.assertNotIn(cur, tradable)
        self.assertIn("BTC", tradable)
        self.assertTrue(all(r.contract == "" for r in rows))

    def test_bithumb_btc_quoted_market_counts_as_tradable(self):
        body = json.dumps({"status": "0000", "data": []}).encode()
        _, tradable, _ = vn.parse_bithumb(body, json.dumps([{"market": "BTC-ETH"}, {"market": "BAD"}]).encode())
        self.assertEqual(tradable, {"ETH"})

    def test_bithumb_error_status_raises(self):
        with self.assertRaises(ValueError):
            vn.parse_bithumb(json.dumps({"status": "5600", "message": "x"}).encode(), b"[]")


class HaltRuleTests(unittest.TestCase):
    def test_gate_finds_cro_and_abs_deposit(self):
        found, counts, evaluable = vn.halts(gate_snapshot())
        self.assertEqual(pairs(found), {("CRO", "deposit"), ("ABS", "deposit")})
        by = {(h.route, h.side): h for h in found}
        cro = by[("CRO", "deposit")]
        self.assertEqual((cro.venue, cro.net), ("gate", "cro"))
        self.assertEqual(cro.listed, 6)  # FER, FUL and MTD are delisted and not counted
        self.assertEqual(cro.closed, ["CAW", "LION", "SINGLE", "VNO", "VVS"])
        abs_ = by[("ABS", "deposit")]
        self.assertEqual((abs_.listed, abs_.closed), (3, ["ETH", "GTBTC", "GUSD"]))
        self.assertIn(("CRO", "withdraw"), evaluable)
        self.assertEqual(counts["halted"], 2)
        self.assertEqual(counts["items"], 33)

    def test_gate_suinew_alias_is_not_reported(self):
        snap = gate_snapshot()
        found, _, evaluable = vn.halts(snap)
        self.assertIn(("SUINEW", "deposit"), evaluable)
        self.assertIn(("SUINEW", "withdraw"), evaluable)
        self.assertNotIn("SUINEW", {h.route for h in found})
        self.assertNotIn("SUI", {h.route for h in found})
        # control: without the open SUI route the very same rows are a halt, so the alias rule is what holds it back
        no_sui = copy.copy(snap)
        no_sui.rows = [r for r in snap.rows if r.route != "SUI"]
        found2, _, _ = vn.halts(no_sui)
        self.assertIn(("SUINEW", "deposit"), pairs(found2))
        self.assertIn(("SUINEW", "withdraw"), pairs(found2))

    def test_kucoin_ton2_alias_is_not_reported(self):
        snap = kucoin_snapshot()
        found, _, evaluable = vn.halts(snap)
        self.assertIn(("ton2", "withdraw"), evaluable)
        self.assertEqual(found, [])
        no_ton = copy.copy(snap)
        no_ton.rows = [r for r in snap.rows if r.route != "ton"]
        found2, _, _ = vn.halts(no_ton)
        self.assertEqual(pairs(found2), {("ton2", "withdraw")})
        self.assertIn("NOT", found2[0].closed)
        self.assertEqual(found2[0].net, "ton")

    def test_route_with_fewer_than_three_tradable_assets_is_not_judged(self):
        rows = [vn.Row("AAA", "XNET", "0xa", False, False), vn.Row("BBB", "XNET", "0xb", False, False),
                # untradable rows do not lift the count
                vn.Row("CCC", "XNET", "0xc", False, False), vn.Row("DDD", "XNET", "0xd", False, False)]
        found, counts, evaluable = vn.halts(snapshot("gate", rows, {"AAA", "BBB"}))
        self.assertEqual(found, [])
        self.assertEqual(evaluable, {})
        self.assertEqual(counts["tradable_routes"], 0)
        # the same route with a third tradable asset is judged and halted
        found, _, evaluable = vn.halts(snapshot("gate", rows, {"AAA", "BBB", "CCC"}))
        self.assertEqual(pairs(found), {("XNET", "deposit"), ("XNET", "withdraw")})

    def test_recorded_route_drops_below_three_when_one_asset_stops_trading(self):
        data = json.loads(fixture("gate_currencies.json"))
        for c in data:
            if c["currency"] == "GTBTC":
                c["trade_disabled"] = True
        found, _, evaluable = vn.halts(gate_snapshot(json.dumps(data).encode()))
        self.assertNotIn(("ABS", "deposit"), evaluable)
        self.assertEqual(pairs(found), {("CRO", "deposit")})

    def test_missing_switches_are_unknown_not_closed(self):
        # Gate: absent deposit_disabled -> None
        body = json.dumps([{"currency": c, "delisted": False, "trade_disabled": False,
                            "chains": [{"name": "YNET", "addr": "0x%d" % i, "withdraw_disabled": False}]}
                           for i, c in enumerate(["AAA", "BBB", "CCC", "DDD"])]).encode()
        rows, tradable, _ = vn.parse_gate(body)
        self.assertTrue(all(r.deposit_open is None and r.withdraw_open is True for r in rows))
        found, _, evaluable = vn.halts(snapshot("gate", rows, tradable))
        self.assertEqual(found, [])
        self.assertNotIn(("YNET", "deposit"), evaluable)
        self.assertIn(("YNET", "withdraw"), evaluable)
        # Binance: null / non-bool switches -> None
        bbody = json.dumps({"data": [{"coin": "AAA", "trading": True, "networkList": [
            {"network": "ZNET", "depositEnable": None, "withdrawEnable": "false"}]}]}).encode()
        brow = vn.parse_binance(bbody)[0][0]
        self.assertEqual((brow.deposit_open, brow.withdraw_open), (None, None))
        # KuCoin: missing keys -> None
        kbody = json.dumps({"data": [{"currency": "AAA", "chains": [{"chainId": "znet"}]}]}).encode()
        krow = vn.parse_kucoin(kbody, json.dumps({"data": []}).encode())[0][0]
        self.assertEqual((krow.deposit_open, krow.withdraw_open), (None, None))
        # Bithumb: anything other than 0/1 -> None
        tbody = json.dumps({"status": "0000", "data": [{"currency": "AAA", "net_type": "ZNET", "deposit_status": "",
                                                        "withdrawal_status": None}]}).encode()
        trow = vn.parse_bithumb(tbody, b"[]")[0][0]
        self.assertEqual((trow.deposit_open, trow.withdraw_open), (None, None))

    def test_unknown_switches_do_not_count_as_shut(self):
        # 2 known-shut + 2 unknown: if unknown counted as closed this would be a 4/4 halt
        rows = [vn.Row("AAA", "XNET", "", False, True), vn.Row("BBB", "XNET", "", False, True),
                vn.Row("CCC", "XNET", "", None, True), vn.Row("DDD", "XNET", "", None, True)]
        found, _, evaluable = vn.halts(snapshot("gate", rows, {"AAA", "BBB", "CCC", "DDD"}))
        self.assertEqual(found, [])
        self.assertNotIn(("XNET", "deposit"), evaluable)

    def test_non_tradable_coins_are_ignored(self):
        rows = [vn.Row(a, "XNET", "", True, True) for a in ("AAA", "BBB", "CCC")]
        rows += [vn.Row(a, "XNET", "", False, False) for a in ("OLD1", "OLD2", "OLD3", "OLD4", "OLD5")]
        found, _, evaluable = vn.halts(snapshot("gate", rows, {"AAA", "BBB", "CCC"}))
        self.assertEqual(found, [])
        self.assertIn(("XNET", "deposit"), evaluable)
        # and they never appear in a halt's asset list
        rows = [vn.Row(a, "XNET", "", False, True) for a in ("AAA", "BBB", "CCC", "OLD1")]
        found, _, _ = vn.halts(snapshot("gate", rows, {"AAA", "BBB", "CCC"}))
        self.assertEqual(len(found), 1)
        self.assertEqual((found[0].listed, found[0].closed), (3, ["AAA", "BBB", "CCC"]))

    def test_close_share_threshold(self):
        # 4 of 5 shut = 0.8 -> halted; 3 of 5 shut -> not halted
        tr = {"A1", "A2", "A3", "A4", "A5"}
        rows = [vn.Row(a, "XNET", "", a == "A5", True) for a in sorted(tr)]
        self.assertEqual(pairs(vn.halts(snapshot("gate", rows, tr))[0]), {("XNET", "deposit")})
        rows = [vn.Row(a, "XNET", "", a in ("A4", "A5"), True) for a in sorted(tr)]
        self.assertEqual(vn.halts(snapshot("gate", rows, tr))[0], [])

    def test_eta_is_the_latest_recovery_estimate_of_the_shut_assets(self):
        rows = [vn.Row("AAA", "XNET", "", False, True, 1000), vn.Row("BBB", "XNET", "", False, True, 5000),
                vn.Row("CCC", "XNET", "", False, True, 0)]
        found, _, _ = vn.halts(snapshot("binance", rows, {"AAA", "BBB", "CCC"}))
        self.assertEqual(found[0].eta_ms, 5000)

    def test_failed_snapshot_judges_nothing(self):
        snap = vn.Snapshot("gate", 1, False, error="HTTPError: 503")
        self.assertEqual(vn.halts(snap), ([], {}, {}))

    def test_recorded_venues_without_halts(self):
        for venue, args in (("binance", [fixture("binance_capital_config.json")]),
                            ("bithumb", [fixture("bithumb_multichain.json"), fixture("bithumb_markets.json")])):
            rows, tradable, items = getattr(vn, "parse_" + venue)(*args)
            found, counts, evaluable = vn.halts(snapshot(venue, rows, tradable, items))
            self.assertEqual(found, [], venue)
            self.assertTrue(evaluable, venue)
        # Binance SEIEVM: SEI is shut there with a recovery estimate, but one asset cannot judge a route
        rows, tradable, _ = vn.parse_binance(fixture("binance_capital_config.json"))
        _, _, evaluable = vn.halts(snapshot("binance", rows, tradable))
        self.assertNotIn(("SEIEVM", "deposit"), evaluable)
        # Binance AVAXC: STG and STGOLD are shut but not tradable, so the route is judged open
        self.assertIn(("AVAXC", "deposit"), evaluable)

    def test_net_names(self):
        self.assertEqual(vn.net_of("SUINEW"), "sui")
        self.assertEqual(vn.net_of("ton2"), "ton")
        self.assertEqual(vn.net_of("ARB_ETH"), "arbitrum")
        self.assertEqual(vn.net_of("CRO"), "cro")


class JudgedTests(unittest.TestCase):
    """halts() third value: (route, side) -> (real shut count, known count, real shut asset names)."""

    def test_judged_counts_only_known_switches(self):
        rows = [vn.Row("AAA", "XNET", "", False, True), vn.Row("BBB", "XNET", "", False, True),
                vn.Row("CCC", "XNET", "", True, True), vn.Row("DDD", "XNET", "", None, True)]
        found, _, judged = vn.halts(snapshot("gate", rows, {"AAA", "BBB", "CCC", "DDD"}))
        self.assertEqual(found, [])
        self.assertEqual(judged[("XNET", "deposit")], (2, 3, frozenset({"AAA", "BBB"})))
        self.assertEqual(judged[("XNET", "withdraw")], (0, 4, frozenset()))

    def test_recorded_gate_routes(self):
        _, _, judged = vn.halts(gate_snapshot())
        self.assertEqual(judged[("CRO", "deposit")], (5, 6, frozenset({"CAW", "LION", "SINGLE", "VNO", "VVS"})))
        self.assertEqual(judged[("ABS", "deposit")], (3, 3, frozenset({"ETH", "GTBTC", "GUSD"})))
        self.assertEqual(judged[("CRO", "withdraw")][0], 0)

    def test_alias_shut_assets_are_not_counted_as_shut(self):
        # Gate SUINEW: every tradable coin is shut there, but all except SUI (no contract) and BLUAI are open on
        # SUI with the same contract, so the engine must not keep a SUINEW halt open on their account.
        snap = gate_snapshot()
        _, _, judged = vn.halts(snap)
        shut, known, names = judged[("SUINEW", "deposit")]
        self.assertEqual(names, frozenset({"SUI", "BLUAI"}))
        self.assertEqual((shut, known), (2, 17))
        self.assertNotIn("HIPPO", names)
        self.assertLessEqual(shut, vn.OPEN_LINE * known)          # judged open: below the open line
        # control: without the open SUI route the same rows are all really shut
        no_sui = copy.copy(snap)
        no_sui.rows = [r for r in snap.rows if r.route != "SUI"]
        _, _, judged2 = vn.halts(no_sui)
        self.assertEqual(judged2[("SUINEW", "deposit")][0], 17)
        self.assertIn("HIPPO", judged2[("SUINEW", "deposit")][2])

    def test_kucoin_ton2_alias_is_judged_open(self):
        _, _, judged = vn.halts(kucoin_snapshot())
        shut, known, names = judged[("ton2", "withdraw")]
        self.assertNotIn("NOT", names)
        self.assertLessEqual(shut, vn.OPEN_LINE * known)


class TradabilityTests(unittest.TestCase):
    def test_binance_trading_must_be_literally_true(self):
        body = json.dumps({"data": [{"coin": c, "trading": v, "networkList": []}
                                    for c, v in (("AAA", True), ("BBB", "true"), ("CCC", 1), ("DDD", None),
                                                 ("EEE", False))]
                          + [{"coin": "FFF", "networkList": []}]}).encode()
        _, tradable, items = vn.parse_binance(body)
        self.assertEqual((tradable, items), ({"AAA"}, 6))

    def test_gate_needs_delisted_and_trade_disabled_both_false(self):
        cases = {"AAA": {"delisted": False, "trade_disabled": False},
                 "BBB": {"trade_disabled": False},                    # delisted missing
                 "CCC": {"delisted": False},                          # trade_disabled missing
                 "DDD": {"delisted": None, "trade_disabled": False},
                 "EEE": {"delisted": "false", "trade_disabled": False},
                 "FFF": {"delisted": 0, "trade_disabled": 0},
                 "GGG": {"delisted": True, "trade_disabled": False},
                 "HHH": {"delisted": False, "trade_disabled": True}}
        body = json.dumps([{"currency": c, "chains": [], **flags} for c, flags in cases.items()]).encode()
        _, tradable, _ = vn.parse_gate(body)
        self.assertEqual(tradable, {"AAA"})


class FakeResponse:
    def __init__(self, body: bytes, gzip_encoded: bool = False):
        self.body = body
        self.headers = {"Content-Encoding": "gzip"} if gzip_encoded else {}

    def read(self, n: int = -1) -> bytes:
        return self.body if n < 0 else self.body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FetchTests(unittest.TestCase):
    URL = "https://venue.invalid/api"

    def fetch(self, resp: FakeResponse, max_bytes: int | None = None) -> bytes:
        with mock.patch.object(vn.urllib.request, "urlopen", return_value=resp) as op, \
                mock.patch.object(vn, "MAX_BYTES", max_bytes or vn.MAX_BYTES):
            out = vn.fetch(self.URL)
        req = op.call_args[0][0]
        self.assertEqual(req.get_header("Accept-encoding"), "gzip")
        return out

    def test_gzip_body_is_decompressed(self):
        data = b'[{"currency": "AAA"}]' * 50
        self.assertEqual(self.fetch(FakeResponse(gzip.compress(data), True)), data)
        self.assertEqual(self.fetch(FakeResponse(data)), data)

    def test_gzip_that_inflates_past_the_cap_is_refused(self):
        bomb = gzip.compress(b"0" * 200_000)
        self.assertLess(len(bomb), 4096)
        with self.assertRaises(ValueError):
            self.fetch(FakeResponse(bomb, True), max_bytes=4096)
        # exactly at the cap still passes
        self.assertEqual(len(self.fetch(FakeResponse(gzip.compress(b"0" * 4096), True), max_bytes=4096)), 4096)

    def test_raw_body_over_the_cap_is_refused(self):
        with self.assertRaises(ValueError):
            self.fetch(FakeResponse(b"x" * 5000), max_bytes=4096)


class PollTests(unittest.TestCase):
    T = 1_800_000_000

    def test_any_fetch_failure_becomes_an_unreadable_snapshot(self):
        for exc in (ConnectionResetError("reset"), http.client.RemoteDisconnected("closed"),
                    http.client.IncompleteRead(b"par"), urllib.error.URLError("timed out"), TimeoutError("slow"),
                    ValueError("response over 16777216 bytes"), zlib.error("bad gzip"), RuntimeError("odd"),
                    KeyError("data")):
            for venue in vn.VENUES:
                with self.subTest(exc=type(exc).__name__, venue=venue), \
                        mock.patch.object(vn, "fetch", side_effect=exc):
                    s = vn.poll(venue, now=lambda: self.T)
                    self.assertFalse(s.ok)
                    self.assertTrue(s.error.startswith(type(exc).__name__ + ":"), s.error)
                    self.assertEqual((s.venue, s.t, s.rows, s.sha256), (venue, self.T, [], ""))
                    self.assertEqual(vn.halts(s), ([], {}, {}))

    def test_malformed_bodies_become_unreadable_snapshots(self):
        bad = {"binance": [b"not json", b"[]", b'{"data": null}', b'{"data": [1]}',
                           b'{"data": [{"coin": "A", "trading": true, "networkList": '
                           b'[{"network": "X", "estimatedRecoveryTime": "soon"}]}]}'],
               "gate": [b'{"a": 1}', b"null", b"[1]", b'[{"currency": "A", "chains": [1]}]'],
               "kucoin": [b'{"data": null}', b'{"code": "200000"}', b"[]"],
               "bithumb": [b"[]", b'{"status": "0000"}', b'{"status": "5600"}']}
        for venue, bodies in bad.items():
            for body in bodies:
                with self.subTest(venue=venue, body=body), mock.patch.object(vn, "fetch", return_value=body):
                    s = vn.poll(venue, now=lambda: self.T)
                    self.assertFalse(s.ok)
                    self.assertTrue(s.error)

    def test_good_bodies_are_hashed_and_sized(self):
        body = fixture("gate_currencies.json")
        with mock.patch.object(vn, "fetch", return_value=body):
            s = vn.poll("gate", now=lambda: self.T)
        self.assertTrue(s.ok)
        self.assertEqual((s.sha256, s.nbytes, s.items, s.t), (hashlib.sha256(body).hexdigest(), len(body), 33, self.T))


if __name__ == "__main__":
    unittest.main()
