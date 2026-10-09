# Door Ledger data contract on Arkiv

Everything Door Ledger stores lives on the Arkiv Tiramisu testnet (chain id 7738577) under `app = 'doorledger'`. Anyone can read it without our server, and anyone can build on it from this page. `python tools/check_schema.py` checks that every attribute the writer sets and every query the page runs is listed here.

## Who writes

| Wallet | Role | Runs on |
|---|---|---|
| `0x65EbE97db5Cd7160bf9a2aa7818241f9E5768A92` | Watcher A (approved) | always-on server, polls every 120 s |
| `0xC10547DBac4E57b89F0f186d79C3a70b1FE533Fb` | Watcher B (approved) | GitHub Actions job about every 30 min (a backup trigger covers slots GitHub's scheduler skips) |
| `0x730E78fc5afB38689fd6b2730DC1E8b59911f71E` | Planted spoof (never approved) | one-off demo of filtering by `$creator` |

The approved list is the trust root (`arkiv/reporters.json`, mirrored in `web/config.js`). Readers keep a row only if its `$creator`, which the chain sets, is approved. Attributes and payloads can be written by anyone and are never used to decide who wrote a row.

## Entity types

Block numbers, not seconds, drive expiry: at 2 s per block, 900 blocks = 30 min, 4,500 = 2.5 h, 7,200 = 4 h, 7,776,000 = 180 days. Every lifetime is sent as `expiresAt = 0, minLifetime = N`, so it counts from the block the transaction lands in.

### `lease`: a halt that is open now, as one watcher sees it

| Attribute | Type | Example | Why it is an attribute |
|---|---|---|---|
| `app` | str | `doorledger` | namespace, in every query |
| `kind` | str | `lease` | selects "Open now" |
| `venue` | str | `gate` | venue filter (`binance`, `gate`, `kucoin`, `bithumb`) |
| `route` | str | `CRO` | the venue's own network id, never a display name |
| `net` | str | `cro` | our normalised network name, for cross-venue filters |
| `side` | str | `deposit` | `deposit` or `withdraw`; a network shut both ways is two leases |
| `t0` | u64 | `1791544850` | watcher clock (unix s) of the first poll that saw it shut |

- **Payload** (`application/json`): `v, venue, route, net, side, t0, rule{min_tradable, close_share, open_line, open_polls, close_polls}, listed, closed, assets[] (shut assets, first 20), eta_unix_ms (Binance's estimated recovery time, unix ms, 0 when absent), src (endpoint URL), first_sha256 / resp_sha256 (hash of the confirming response bodies), code (writer git sha), coverage (continuous or sampled), resumed_from (previous lease key, when a lapsed halt is re-leased)`.
- **Expiry:** 900 blocks for Watcher A, 7,200 for Watcher B, renewed with `extend` only while the watcher still sees the halt. If the watcher stops or loses sight of it, the lease lapses and the halt leaves "Open now" with no cleanup job.
- **Flags:** read-only (1). Contents never change; a new count is a new lease.
- **Owner:** the watcher, which renews it and deletes it when the halt closes.

### `episode`: a closed halt, the ledger row

| Attribute | Type | Example | Why it is an attribute |
|---|---|---|---|
| `app`, `kind` | str | `doorledger`, `episode` | namespace, selects the ledger |
| `venue`, `route`, `net`, `side` | str | as in `lease` | ledger filters |
| `t0` | u64 | `1791544850` | start; date filters |
| `t1` | u64 | `1791554606` | first poll that saw it reopen |
| `dur_s` | u64 | `9756` | `t1 - t0`; "lasted at least 6 h" is `dur_s >= u64(21600)`. Stored because queries have no arithmetic |

- **Payload:** `v, venue, route, net, side, t0, t1, dur_s, rule, listed, max_closed, assets[], eta_unix_ms, lease_key, lease_tx (the transaction that opened the halt, still on the explorer after the lease is gone), first_sha256, last_sha256, src, code, coverage`, plus `polls` and `gaps[[from, to]]` (watcher blind spells, honest coverage) for Watcher A.
- **Expiry:** 7,776,000 blocks (180 days), extendable by anyone.
- **Flags:** read-only + permissionless extension (3).
- **Owner:** `0x000000000000000000000000000000000000dEaD`, set by a transfer in the same transaction that creates it. After that the watcher that wrote it can no longer delete or edit it (live check: `tools/burn_check.py`, evidence `arkiv/evidence/burn-check-2026-10-09.json`), while anyone can still extend its life.
- **Close batch (atomic):** `execute([create(episode), transfer(episode -> 0x...dEaD), delete(lease)])`. Either the ledger row exists and the open lease is gone, or nothing changed.

### `pulse`: proof that a watcher is alive and reading a venue

| Attribute | Type | Example | Why it is an attribute |
|---|---|---|---|
| `app`, `kind` | str | `doorledger`, `pulse` | namespace, selects the Watchers table |
| `venue` | str | `kucoin` | one pulse per venue |

- **Payload:** `v, venue, t, polls_ok, polls_fail (reads since the previous pulse), items, routes, tradable_routes, halted, halted_routes[], resp_sha256, resp_bytes, src, code, coverage`.
- **Expiry:** 4,500 blocks for Watcher A (pulses hourly), 5,400 for Watcher B (at most hourly). A venue whose pulse has expired is shown as silent, so an empty "Open now" is never mistaken for a quiet market.
- **Flags:** read-only (1). **Owner:** the watcher. Never extended, never deleted.

### `selftest`

One entity from `tools/burn_check.py` (attributes `app`, `kind = 'selftest'`, `check`), kept as public evidence of the burn lock. Readers ignore it because every query names a kind.

## Halt rule

A route (venue, network id, side) is halted when at least 3 tradable assets are listed on it and at least 80 % of them are shut. Shut assets that are open on another route of the same venue with the same contract are aliases, not a halt. A halt opens after 2 consecutive halted polls and closes after 3 consecutive polls at or below 40 % shut (or with every originally shut asset open again). A poll that cannot judge the route (venue unreadable, response much smaller than usual, route missing) changes nothing. Only confirmed changes reach Arkiv.

## Queries the page runs

Constants (lowercase addresses):

```
APP      = app = str('doorledger')
APPROVED = ($creator = addr(0x65ebe97db5cd7160bf9a2aa7818241f9e5768a92) OR $creator = addr(0xc10547dbac4e57b89f0f186d79c3a70b1fe533fb))
SELECT   = {"key":true,"creator":true,"owner":true,"createdAt":true,"expiresAt":true,"attributes":true,"payload":true}
```

| Query | Used for |
|---|---|
| `app = str('doorledger') AND (kind = str('lease') OR kind = str('pulse')) AND APPROVED` | Open now and Watchers, one query, pages of 200 (up to 5) pinned to one block |
| the same without `AND APPROVED` | the switch "Approved watchers only" turned off (spoof rows appear, tagged) |
| `app = str('doorledger') AND kind = str('episode') AND APPROVED` | Ledger, page size 20, newest first |
| ledger filters appended: `AND venue = str('gate')`, `AND side = str('deposit')`, `AND dur_s >= u64(21600)`, `AND t0 >= u64(<unix s>)`, `AND t0 < u64(<unix s>)` | Ledger filters |
| `arkiv_getEntityCount` with the ledger query and `block` | "Showing n of m" |
| `$key = key(<entity key>)` | one record, from the detail view |

- **Pagination:** page 2 and later repeat the query and select with `atBlock` = page 1's `blockNumber` and its `cursor`. Without `atBlock` a cursor fails once the chain moves (friction item 2).
- **As of a past block:** every query above runs with `atBlock = <block>`. Leases that were open then show up although they are gone now.
- **Numbers are always tagged** `u64(...)`: an untagged number is an i32 and silently matches nothing against these attributes (friction item 1).
- **Live updates:** a WebSocket `eth_subscribe` to `newHeads` (ages rows and drops expired leases, since expiry emits no event) and to `logs` from `0x4400000000000000000000000000000000000044` with the watcher addresses as the owner topic; each new or changed key is fetched with `arkiv_getEntity`, which costs no query budget.

## Build on it: examples

**A wallet that warns before a user sends to a shut deposit route.** Before showing a Gate deposit address for USDT on Tron, ask whether trusted watchers currently see that route shut:

```
app = str('doorledger') AND kind = str('lease') AND venue = str('gate') AND net = str('tron') AND side = str('deposit') AND ($creator = addr(0x65ebe97db5cd7160bf9a2aa7818241f9e5768a92) OR $creator = addr(0xc10547dbac4e57b89f0f186d79c3a70b1fe533fb))
```

Any row means "a trusted watcher saw this network's deposits shut within its lease life"; the payload's `assets` says whether USDT is among the shut assets.

**A reliability score per venue.** Count closed halts per venue over a window and how long they lasted:

```
app = str('doorledger') AND kind = str('episode') AND venue = str('binance') AND t0 >= u64(1790812800) AND <trusted creators>
```

`tools/consumer_example.py` is a working consumer written only from this page (standard library, no Door Ledger code).

## Versioning

- Every payload carries `"v": 1`. Within version 1, fields are only ever added, never renamed, removed or re-typed; readers must ignore fields they do not know.
- Attribute names and types listed above are fixed for version 1. A breaking change would use a new `app` value (for example `doorledger2`), so old readers keep working against old data.
- `rule` in every lease and episode payload records the thresholds that produced it, so rows written under a future rule change stay interpretable.

## Example: reproduce the ledger with curl

```
curl -s https://rpc.tiramisu.db-chain.testnet.arkiv.network -H 'content-type: application/json' --data-binary @- <<'EOF'
{"jsonrpc":"2.0","id":1,"method":"arkiv_query","params":["app = str('doorledger') AND kind = str('episode') AND ($creator = addr(0x65ebe97db5cd7160bf9a2aa7818241f9e5768a92) OR $creator = addr(0xc10547dbac4e57b89f0f186d79c3a70b1fe533fb))",{"limit":20,"select":{"key":true,"creator":true,"owner":true,"createdAt":true,"expiresAt":true,"attributes":true,"payload":true}}]}
EOF
```

The page's "Verify it yourself" section prints the same command pinned to the block its tables were answered at.

## What stays off Arkiv

Prices and order books, the raw exchange responses (only their sha256), the per-asset switch flips that never confirm a network halt, alert logic, and any personal data. The exchanges' responses are large, third-party content and change every poll; a hash is enough to show which response a row was based on.
