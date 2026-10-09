"""Read each exchange's public deposit/withdraw switches and reduce them to network-level halts.

Every source is a keyless public endpoint. None of them says *when* a switch flipped, so all times
are the watcher's own clock.

A route is the venue's own network id (Binance `network`, Gate `chains[].name`, KuCoin `chainId`,
Bithumb `net_type`). Display names are never used as keys: KuCoin shows `TON` for both `ton` and
`ton2`, and keying by that name invents a halt.

Network rule (per venue, route and side):
  - count only tradable assets on the route;
  - the route is HALTED when at least MIN_ASSETS of them are listed and at least CLOSE_SHARE are shut;
  - a shut asset that is open on another route of the same venue with the same contract is an alias,
    not a halt (Gate `SUINEW` vs `SUI`, KuCoin `ton2` vs `ton`); if ALIAS_SHARE of the shut assets
    are aliases the route is not halted;
  - anything missing from a response is unknown, never "closed".
"""
from __future__ import annotations

import gzip
import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

MIN_ASSETS = 3
CLOSE_SHARE = 0.8
ALIAS_SHARE = 0.8
UA = "door-ledger/0.1 (+https://github.com/bongbongcrypto/door-ledger)"
MAX_BYTES = 16 * 1024 * 1024

URLS = {
    "binance": ["https://www.binance.com/bapi/capital/v1/public/capital/getNetworkCoinAll"],
    "gate": ["https://api.gateio.ws/api/v4/spot/currencies"],
    "kucoin": ["https://api.kucoin.com/api/v3/currencies", "https://api.kucoin.com/api/v2/symbols"],
    "bithumb": ["https://api.bithumb.com/public/assetsstatus/multichain/ALL",
                "https://api.bithumb.com/v1/market/all?isDetails=false"],
}
VENUES = tuple(URLS)

# Route id (upper case) -> our network name, so one network can be filtered across venues.
# Anything not listed keeps its own lower-cased route id.
NET_ALIASES = {
    "ETH": "ethereum", "ERC20": "ethereum", "BSC": "bsc", "BEP20": "bsc", "SOL": "solana", "TRX": "tron",
    "TRC20": "tron", "MATIC": "polygon", "POL": "polygon", "POLYGON": "polygon", "ARBITRUM": "arbitrum",
    "ARBEVM": "arbitrum", "ARB_ETH": "arbitrum", "ARBONE": "arbitrum", "OPTIMISM": "optimism", "OPETH": "optimism",
    "OP_ETH": "optimism", "BASE": "base", "BASEEVM": "base", "BASE_ETH": "base", "AVAXC": "avalanche",
    "AVAX_C": "avalanche", "AVAX": "avalanche", "TON": "ton", "TON2": "ton", "SUI": "sui", "SUINEW": "sui",
    "APT": "aptos", "APTOS": "aptos", "NEAR": "near", "DOT": "polkadot", "STATEMINT": "polkadot-assethub",
    "KAIA": "kaia", "KLAY": "kaia", "STARKNET": "starknet", "RON": "ronin", "RONIN": "ronin", "SEI": "sei",
    "SEIEVM": "sei-evm", "LINEA": "linea", "ZKSYNCERA": "zksync", "ZKSYNC": "zksync", "SCROLL": "scroll",
    "MANTLE": "mantle", "OPBNB": "opbnb", "CELO": "celo", "FTM": "fantom", "SONIC": "sonic", "HYPE": "hyperliquid",
    "HYPEREVM": "hyperevm", "XRP": "xrp", "ADA": "cardano", "ALGO": "algorand", "ATOM": "cosmos", "BTC": "bitcoin",
}


def net_of(route: str) -> str:
    return NET_ALIASES.get(route.upper(), route.lower())


@dataclass
class Row:
    asset: str
    route: str
    contract: str
    deposit_open: bool | None
    withdraw_open: bool | None
    eta_ms: int = 0


@dataclass
class Snapshot:
    venue: str
    t: int                      # watcher unix seconds when the poll finished
    ok: bool
    rows: list[Row] = field(default_factory=list)
    tradable: set[str] = field(default_factory=set)
    items: int = 0              # top-level items in the main response (short-response guard)
    sha256: str = ""
    nbytes: int = 0
    error: str = ""


@dataclass
class RouteHalt:
    venue: str
    route: str
    net: str
    side: str                   # deposit | withdraw
    listed: int                 # tradable assets on the route
    closed: list[str]           # tradable assets shut on this side
    eta_ms: int = 0


def fetch(url: str, timeout: float = 30.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json",
                                               "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("response over %d bytes" % MAX_BYTES)
        if r.headers.get("Content-Encoding", "").lower() == "gzip":
            raw = gzip.decompress(raw)
    return raw


# ---------------------------------------------------------------------------------------------
# Parsers: bytes -> rows (pure functions, tested with recorded fixtures)
# ---------------------------------------------------------------------------------------------

def parse_binance(body: bytes) -> tuple[list[Row], set[str], int]:
    data = json.loads(body)["data"]
    rows, tradable = [], set()
    for c in data:
        coin = str(c.get("coin", "")).upper()
        if c.get("isLegalMoney"):
            continue
        if c.get("trading", True):
            tradable.add(coin)
        for n in c.get("networkList") or []:
            rows.append(Row(coin, str(n.get("network", "")), str(n.get("contractAddress") or "").lower(),
                            _bool(n.get("depositEnable")), _bool(n.get("withdrawEnable")),
                            int(n.get("estimatedRecoveryTime") or 0)))
    return rows, tradable, len(data)


def parse_gate(body: bytes) -> tuple[list[Row], set[str], int]:
    data = json.loads(body)
    rows, tradable = [], set()
    for c in data:
        cur = str(c.get("currency", "")).upper()
        if not c.get("delisted") and not c.get("trade_disabled"):
            tradable.add(cur)
        for ch in c.get("chains") or []:
            name = str(ch.get("name", ""))
            if not name or name == "ITSNOTACHAIN":
                continue
            dep, wd = ch.get("deposit_disabled"), ch.get("withdraw_disabled")
            rows.append(Row(cur, name, str(ch.get("addr") or "").lower(),
                            None if dep is None else not dep, None if wd is None else not wd))
    return rows, tradable, len(data)


def parse_kucoin(body: bytes, symbols: bytes) -> tuple[list[Row], set[str], int]:
    data = json.loads(body)["data"]
    tradable = {str(s.get("baseCurrency", "")).upper() for s in json.loads(symbols)["data"] if s.get("enableTrading")}
    rows = []
    for c in data:
        cur = str(c.get("currency", "")).upper()
        for ch in c.get("chains") or []:
            rows.append(Row(cur, str(ch.get("chainId") or ""), str(ch.get("contractAddress") or "").lower(),
                            _bool(ch.get("isDepositEnabled")), _bool(ch.get("isWithdrawEnabled"))))
    return rows, tradable, len(data)


def parse_bithumb(body: bytes, markets: bytes) -> tuple[list[Row], set[str], int]:
    d = json.loads(body)
    if d.get("status") != "0000":
        raise ValueError("bithumb status %s" % d.get("status"))
    tradable = set()
    for m in json.loads(markets):
        parts = str(m.get("market", "")).split("-")
        if len(parts) == 2:
            tradable.add(parts[1].upper())
    rows = [Row(str(x.get("currency", "")).upper(), str(x.get("net_type", "")), "",
                _flag(x.get("deposit_status")), _flag(x.get("withdrawal_status"))) for x in d["data"]]
    return rows, tradable, len(d["data"])


def _bool(v) -> bool | None:
    return v if isinstance(v, bool) else None


def _flag(v) -> bool | None:
    if v in (1, "1"):
        return True
    if v in (0, "0"):
        return False
    return None


def poll(venue: str, now=time.time) -> Snapshot:
    urls = URLS[venue]
    try:
        bodies = [fetch(u) for u in urls]
        if venue == "binance":
            rows, tradable, items = parse_binance(bodies[0])
        elif venue == "gate":
            rows, tradable, items = parse_gate(bodies[0])
        elif venue == "kucoin":
            rows, tradable, items = parse_kucoin(bodies[0], bodies[1])
        else:
            rows, tradable, items = parse_bithumb(bodies[0], bodies[1])
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, TypeError, json.JSONDecodeError, OSError) as e:
        return Snapshot(venue, int(now()), False, error="%s: %s" % (type(e).__name__, str(e)[:200]))
    return Snapshot(venue, int(now()), True, rows, tradable, items, hashlib.sha256(bodies[0]).hexdigest(),
                    len(bodies[0]))


# ---------------------------------------------------------------------------------------------
# Rule: rows -> halted routes
# ---------------------------------------------------------------------------------------------

def halts(snap: Snapshot) -> tuple[list[RouteHalt], dict, set]:
    """Halted (route, side) pairs in one snapshot, counts for the venue pulse, and the set of
    (route, side) pairs this snapshot could judge at all (enough tradable assets with a known switch)."""
    if not snap.ok:
        return [], {}, set()
    by_route: dict[str, list[Row]] = {}
    open_routes: dict[str, dict[tuple[str, str], set[str]]] = {"deposit": {}, "withdraw": {}}  # side -> (asset, contract) -> routes
    for r in snap.rows:
        if r.asset not in snap.tradable or not r.route:
            continue
        by_route.setdefault(r.route, []).append(r)
    for r in snap.rows:
        if r.contract:
            if r.deposit_open:
                open_routes["deposit"].setdefault((r.asset, r.contract), set()).add(r.route)
            if r.withdraw_open:
                open_routes["withdraw"].setdefault((r.asset, r.contract), set()).add(r.route)
    found, tradable_routes, evaluable = [], 0, set()
    for route, rows in by_route.items():
        if len(rows) >= MIN_ASSETS:
            tradable_routes += 1
        for side in ("deposit", "withdraw"):
            known = [r for r in rows if (r.deposit_open if side == "deposit" else r.withdraw_open) is not None]
            if len(known) < MIN_ASSETS:
                continue
            evaluable.add((route, side))
            shut = [r for r in known if not (r.deposit_open if side == "deposit" else r.withdraw_open)]
            if len(shut) < CLOSE_SHARE * len(known):
                continue
            aliased = [r for r in shut if r.contract
                       and open_routes[side].get((r.asset, r.contract), set()) - {route}]
            if shut and len(aliased) >= ALIAS_SHARE * len(shut):
                continue
            eta = max((r.eta_ms for r in shut), default=0)
            found.append(RouteHalt(snap.venue, route, net_of(route), side, len(known),
                                   sorted({r.asset for r in shut}), eta))
    counts = {"items": snap.items, "routes": len(by_route), "tradable_routes": tradable_routes,
              "halted": len(found)}
    return found, counts, evaluable
