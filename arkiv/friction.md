# Arkiv friction report

What we ran into while building Door Ledger on Arkiv, and what worked exactly as documented. Every item below was reproduced on the date shown by `tools/friction_probe.py`; the raw requests, response excerpts and headers are in `arkiv/evidence/friction-2026-10-09.json`. Nothing here comes from memory alone: if the probe could not re-run it today, it is not listed (see the end). The one exception is an earlier HTTP 429 in item 3, marked as such.

| | |
|---|---|
| Date | 2026-10-09, probe run 12:08:36 to 12:09:17 UTC |
| Network | Tiramisu testnet, chain id 7738577, `https://rpc.tiramisu.db-chain.testnet.arkiv.network` |
| Node | `web3_clientVersion` = `reth/v2.5.0-189c0df/x86_64-unknown-linux-gnu`; head 348990 to 348999 during the run |
| How we connect | Direct JSON-RPC over HTTPS and WSS, no SDK. Writes are `execute()` calldata built with eth_abi 6.0.0 and signed with eth_account 0.13.7 (Python 3.12.10). WebSocket client: websockets 15.0.1 |
| Docs compared | Arkiv-Network/arkiv-starlight-docs at commit 45099f7 (default branch `develop`; `main` at d64a177 has identical content for every file cited). Paths are relative to `src/content/docs/` |
| Node source | Arkiv-Network/arkiv at a965df7, read only to explain a result. Every result itself comes from the live node |
| Re-run | `python tools/friction_probe.py` |

The probe only reads and simulates. Writes are tested with `eth_estimateGas` or `eth_call` from `0x000000000000000000000000000000000000beef`, which needs no key. It never signs or sends a transaction and opens no key file. Today's run made 52 HTTP requests, 15 of them `arkiv_query`.

Steps below give the JSON-RPC request body. POST it to the RPC URL with `content-type: application/json` and a non-default User-Agent (see item 6). Simulated batches are written as `execute([op, ...])`; the exact calldata of each is in the evidence file under that item's `calls[].params`.

## Summary, ordered by impact

| # | Item | Type | Probe id | Today |
|---|---|---|---|---|
| 1 | Untagged number against a u64 attribute: 0 rows, no error | Friction | `untagged_number` | reproduced |
| 2 | The JSON-RPC pagination example fails once the head moves | Friction | `cursor_without_atblock` | reproduced |
| 3 | Anonymous query cost is not documented | Friction | `rate_headers` | reproduced |
| 4 | Reserved words accepted as attribute names on write | Friction | `reserved_word_name` | reproduced |
| 5 | Uppercase names: the write reverts, the query is silently empty | Friction | `uppercase_name` | reproduced |
| 6 | Python's default HTTP client is refused with HTTP 403 | Friction | `default_user_agent` | reproduced |
| 7 | One block parameter, two encodings, Rust debug text in errors | Friction | `block_param_encoding` | reproduced |
| 8 | Unix seconds in `expiresAt` accepted without a bound | Friction | `unix_seconds_expiry` | reproduced |
| 9 | `$updatedAt`: two docs pages disagree | Friction (docs) | `updated_at_queryable` | reproduced |
| 10 | An expired entity can still be deleted | Friction (docs) | `expired_entity_ops` | reproduced |
| 11 | Extending to the same expiry is accepted | Friction (docs) | `equal_expiry_extend` | reproduced |
| 12 | Expiry emits no event, reads hide the entity at `expiresAt` | Worked as documented | `expiry_has_no_event` | pass |
| 13 | Read-only blocks patch, not delete | Worked as documented | `readonly_flag` | pass |
| 14 | Permissionless extension by a third party | Worked as documented | `permissionless_extend` | pass |
| 15 | `execute()` calldata from eth_abi equals the docs example | Worked as documented | `calldata_matches_docs` | pass |
| 16 | Entity key prediction | Worked as documented | `key_prediction` | pass |
| 17 | Browser access: CORS and WebSocket subscriptions | Worked as documented | `browser_access` | pass |

---

## Friction

### 1. Untagged number against a u64 attribute: 0 rows, no error

- **Surface:** `arkiv_query` predicate typing.
- **Expected:** `json-rpc/querying-data.mdx:93-95` says every value carries a type tag that is part of what the predicate asserts, and that a bare number is an `i32`. The first query example on that page is `priority >= 3` (line 73), while the create example in `json-rpc/mutating-entities.mdx:246-257` stores `count` as a `u64` (typeId 3). Docs silent on what a mismatch looks like.
- **Actual:** against a live entity with a u64 attribute, the tagged range matched 1 row and the untagged one matched 0 rows with no error. The node already returns a typed error in two neighbouring cases:

  | Query (same block 348991) | Result |
  |---|---|
  | `$key = key(0x351e...7deb) AND random_number_0_fddf30e51a2ea977 >= u64(0)` | 1 row |
  | `$key = key(0x351e...7deb) AND random_number_0_fddf30e51a2ea977 >= 0` | 0 rows, no error |
  | `app >= str('a')` | -32002 `"str values have no ordering \u2014 only i32, u64, u256 and dec support < <= > >="` |
  | `$createdAt >= 0` | -32002 `"this system attribute holds u64 \u2014 write u64(0)"` |

- **Steps to reproduce:** take any live entity with a u64 attribute (the probe prefers a Door Ledger row with `t0`; none existed yet, so today it took the first u64 attribute on page 1 of `*`, from a baseload test entity), then:
  ```json
  {"jsonrpc":"2.0","id":1,"method":"arkiv_query","params":["*",{"limit":20,"select":{"key":true,"attributes":true}}]}
  {"jsonrpc":"2.0","id":2,"method":"arkiv_query","params":["$key = key(<KEY>) AND <NAME> >= u64(0)",{"limit":1,"atBlock":"<B>"}]}
  {"jsonrpc":"2.0","id":3,"method":"arkiv_query","params":["$key = key(<KEY>) AND <NAME> >= 0",{"limit":1,"atBlock":"<B>"}]}
  {"jsonrpc":"2.0","id":4,"method":"arkiv_query","params":["$createdAt >= 0",{"limit":1}]}
  ```
- **Versions:** node reth/v2.5.0-189c0df on Tiramisu 7738577, block 348991, 2026-10-09 12:08:39 UTC; docs 45099f7; raw JSON-RPC, no SDK.
- **Suggested fix:** when a numeric literal's type matches no indexed value of that attribute but another numeric type does, return -32002 (as `$createdAt` already does) or a `warnings` entry in the result, for example "t0 is stored as u64 here; write u64(21600)". In the docs, tag the literal in the first example and add a caution that a mismatched tag returns no rows rather than an error.
- **Impact on Door Ledger:** all numeric attributes (`t0`, `t1`, `dur_s`) are written through the `u64()` helper in `writer/arkiv.py`, and every numeric filter the reader builds is written as `u64(...)` (`web/app.js`, `histQuery`). A "6 hours or longer" filter written as `dur_s >= 21600` would have shown an empty history with no hint.

### 2. The JSON-RPC pagination example fails once the head moves

- **Surface:** `arkiv_query` cursors.
- **Expected:** `json-rpc/querying-data.mdx:270-284` says to pass the returned cursor in the next request. Its example sends only `cursor` and `limit` (no `atBlock`) and shows the cursor as `"0x2a"`. Line 140 says `atBlock` defaults to the head. The JSON-RPC page is silent on cursors being tied to a block. The TypeScript page (`typescript-sdk/querying-data.mdx:322-324`) says every later page reads the block page 1 was read at, but the JSON-RPC page never says how a raw client gets that behaviour.
- **Actual:** page 1 answered at block 348994 (`0x55342`) with cursor `b64:lyd3S4nKd8sAAAAAAAdgqA`. Three seconds later (head 348995), page 2 sent exactly as in the docs failed:
  `{"code":-32005,"message":"cursor belongs to a different query, block or select \u2014 start a new page-through"}`.
  The same request plus `"atBlock":"0x55342"` returned the next row and a new cursor. With 2 s blocks, any page 2 sent more than about 2 s after page 1 fails. The node resolves the block before it checks the cursor (`crates/arkiv-reth-rpc/src/lib.rs:278-285` at a965df7). Cursors look like `b64:...`, not `0x2a`.
  Source reading only, not executed: in arkiv-sdk-js at 3714544 (0.8.1-dev.1), `next()` re-sends the first request with the new cursor and adds `atBlock` only when the caller set one (`src/query/queryBuilder.ts:171,199-212`, `src/query/engine.ts:58`), so SDK users may hit the same error.
- **Steps to reproduce:**
  ```json
  {"jsonrpc":"2.0","id":1,"method":"arkiv_query","params":["*",{"limit":"0x1"}]}
  ```
  wait for one new block (`eth_blockNumber` greater than page 1's `blockNumber`), then
  ```json
  {"jsonrpc":"2.0","id":2,"method":"arkiv_query","params":["*",{"cursor":"<cursor from page 1>","limit":"0x1"}]}
  {"jsonrpc":"2.0","id":3,"method":"arkiv_query","params":["*",{"cursor":"<cursor from page 1>","limit":"0x1","atBlock":"<blockNumber from page 1>"}]}
  ```
- **Versions:** node reth/v2.5.0-189c0df on Tiramisu 7738577, blocks 348994 to 348995, 2026-10-09 12:08:46 to 12:08:50 UTC; docs 45099f7; raw JSON-RPC, no SDK.
- **Suggested fix:** when `atBlock` is absent and a cursor is present, take the block from the cursor (it already carries a binding to that block; carrying the number itself would make cursors self-contained). Failing that, change the docs example to copy `blockNumber` from page 1 into `atBlock` and show a real `b64:` cursor.
- **Impact on Door Ledger:** the reader's "Load more" sends `atBlock` set to page 1's `blockNumber` together with the cursor, and the total count is asked at that same block (`web/app.js`, `loadHistory`). The writer's start-up recovery pages its own leases the same way (`writer/engine.py`, `load`).

### 3. Anonymous query cost is not documented

- **Surface:** public RPC rate limiting and cost accounting.
- **Expected:** `start-here/access-keys.mdx:13-15` says a key has a monthly quota measured in cost units and that without a key the public RPC works at a default rate limit. `cookbook/user-profiles.mdx:71` says anonymous access is rate limited. Docs silent on the default limit, on what each method costs, and on whether anonymous callers have a cost budget.
- **Actual:** every JSON-RPC response carried `ratelimit: limit=600, remaining=..., reset=...` (a 600-request window; `reset` counted down from 60). Only `arkiv_query` carried `arkiv-cost: 100`, on all 15 calls, including the 7 the node rejected (-32001, -32002, -32005, -32602). `arkiv_getEntity`, `arkiv_getEntityCount`, `eth_getLogs`, `eth_estimateGas`, `eth_call` and `eth_blockNumber` carried no cost header. `remaining` was not monotonic across consecutive responses (582, 589, 585, 581, 588, ...). The CORS `access-control-expose-headers` list names `Arkiv-Quota-Used-Percent`, but no anonymous response carried it.
  Earlier observation, not reproduced today on purpose (re-triggering it would drain the shared anonymous budget): on 2026-10-08 at 20:55 UTC, after roughly 85 `arkiv_query` calls in about 25 minutes from one machine, queries got HTTP 429 with body `{"error":"ANON_COST_LIMITED","message":"unauthenticated query budget exhausted; get an API key at https://hub.arkiv.network for a monthly quota"}` and headers `ratelimit: limit=10000, remaining=0, reset=2053`, `retry-after: 2053`. At 100 units per query that is a budget of about 100 queries per window, and it showed in `ratelimit` only once it was the bucket refusing calls.
- **Steps to reproduce:** send any `arkiv_query` and any `eth_blockNumber`, and compare the `arkiv-cost` and `ratelimit` response headers, for example
  ```json
  {"jsonrpc":"2.0","id":1,"method":"arkiv_query","params":["$updatedAt >= u64(0)",{"limit":1}]}
  ```
  (rejected with -32002, still `arkiv-cost: 100`). The probe's `rate_headers` item tabulates the headers of a whole run.
- **Versions:** node reth/v2.5.0-189c0df on Tiramisu 7738577, blocks 348990 to 348999, 2026-10-09 12:08:36 to 12:09:17 UTC; docs 45099f7.
- **Suggested fix:** document the anonymous limits (requests per window and cost units per window, per IP or shared), the cost of each method, and that rejected queries are charged. Expose the cost bucket in `ratelimit` (or `Arkiv-Quota-Used-Percent`) before it runs out, so a client can slow down instead of finding out from a 429.
- **Impact on Door Ledger:** the reader is a static page, so every visitor spends their own anonymous budget. First load makes 2 `arkiv_query` calls (open leases and pulses; first history page). The total count comes from `arkiv_getEntityCount`, changed rows are re-read with `arkiv_getEntity`, live changes arrive over the WebSocket, and nothing polls. On HTTP 429 the page keeps its rows, shows a banner and retries after `Retry-After`, at most 60 s (`web/app.js`, `rpc`). The writers query only at start-up, one page per 200 rows (`writer/engine.py`, `load`), and honour `Retry-After` (`writer/arkiv.py`, `Rpc.call`).

### 4. Reserved words accepted as attribute names on write

- **Surface:** attribute names in `execute()` create, and the query parser.
- **Expected:** `json-rpc/mutating-entities.mdx:129` lists reserved words such as `and`, `not`, `str` and `u64`, and says: "The node rejects those as attribute names."
- **Actual:** a create with three `str` attributes named `and`, `key` and `str` simulated fine (95,960 gas). No query can name them afterwards:
  `and = str('x')` gives -32001 `"a reserved word cannot be used as an attribute name"`, and `str = str('x')` gives -32001 `"str is a type name and cannot be an attribute name"`. The write path checks only the character set (`crates/arkiv-bindings/src/types/ident32.rs`); reserved words are checked only by the query parser (`crates/arkiv-query/src/parse.rs:446-469`).
- **Steps to reproduce:**
  `eth_estimateGas {"from":"0x000000000000000000000000000000000000beef","to":"0x4400000000000000000000000000000000000044","data": execute([create(salt 0xa2, expiresAt 0, minLifetime 100, flags 0, [and: str "x", key: str "x", str: str "x"])])}`, then
  ```json
  {"jsonrpc":"2.0","id":1,"method":"arkiv_query","params":["and = str('x')",{"limit":1}]}
  {"jsonrpc":"2.0","id":2,"method":"arkiv_query","params":["str = str('x')",{"limit":1}]}
  ```
- **Versions:** node reth/v2.5.0-189c0df on Tiramisu 7738577, block 348991, 2026-10-09 12:08:44 UTC; docs 45099f7; eth_abi 6.0.0, no SDK.
- **Suggested fix:** reject reserved words and type names at write time with a dedicated revert, so data can never be stored under a name no query can reach. If that is not wanted, change the docs sentence to say queries cannot reference such names.
- **Impact on Door Ledger:** `writer/arkiv.py` keeps its own copy of the reserved words and refuses to encode them (`_RESERVED`, `_name32`).

### 5. Uppercase names: the write reverts, the query is silently empty

- **Surface:** attribute names on write versus in queries.
- **Expected:** `json-rpc/mutating-entities.mdx:129` describes a name as a letter followed by letters, digits, `.`, `-` or `_`, and says names are case-sensitive. Read plainly, `Kind` is a valid name distinct from `kind`.
- **Actual:** the same create simulated with `kind` succeeds (85,920 gas) and with `Kind` reverts `Ident32InvalidByte(0, 0x4b)` (byte 0 is `K`). Names are lowercase only (`crates/arkiv-bindings/src/types/ident32.rs:4`). The query parser accepts `[A-Za-z]` (`crates/arkiv-query/src/parse.rs:446`), so a capitalised name, which can never be stored, returns 0 rows and no error: `... AND Random_number_0_fddf30e51a2ea977 >= u64(0)` gave 0 rows at the block where the lowercase name gave 1.
- **Steps to reproduce:**
  `eth_estimateGas` from `0x...beef` of `execute([create(salt 0xa1, expiresAt 0, minLifetime 100, flags 0, [Kind: str "x"])])`, and the same with `kind`; then
  ```json
  {"jsonrpc":"2.0","id":1,"method":"arkiv_query","params":["$key = key(<KEY>) AND <Name> >= u64(0)",{"limit":1,"atBlock":"<B>"}]}
  ```
  with the entity and attribute from item 1, first letter capitalised.
- **Versions:** node reth/v2.5.0-189c0df on Tiramisu 7738577, block 348991, 2026-10-09 12:08:42 UTC; docs 45099f7; eth_abi 6.0.0, no SDK.
- **Suggested fix:** in the docs, say "a lowercase letter, then lowercase letters, digits, `.`, `-` or `_`". In the query parser, reject names that cannot be stored with -32001, as it already does for reserved words.
- **Impact on Door Ledger:** `writer/arkiv.py` checks every name against `^[a-z][a-z0-9._-]{0,31}$` before encoding, and the reader's query strings use only those names.

### 6. Python's default HTTP client is refused with HTTP 403

- **Surface:** the HTTPS edge in front of the RPC.
- **Expected:** docs silent. The JSON-RPC pages describe plain JSON-RPC 2.0 over HTTP and use curl in every example.
- **Actual:** the same `eth_blockNumber` POST sent with Python's standard library and its default `User-Agent: Python-urllib/3.12` got HTTP 403, body `error code: 1010`, `server: cloudflare` (Cloudflare error code 1010). With `User-Agent: door-ledger-friction-probe/1` it got 200. We first met it in our read-only research calls before the opening, which is why our client sent its own User-Agent from its first commit; the probe reproduces it.
- **Steps to reproduce:**
  ```python
  import json, urllib.request
  body = json.dumps({"jsonrpc": "2.0", "id": 0, "method": "eth_blockNumber", "params": []}).encode()
  url = "https://rpc.tiramisu.db-chain.testnet.arkiv.network"
  urllib.request.urlopen(urllib.request.Request(url, body, {"Content-Type": "application/json"}))  # HTTPError 403
  urllib.request.urlopen(urllib.request.Request(url, body, {"Content-Type": "application/json",
                                                            "User-Agent": "my-app/1"}))          # 200
  ```
- **Versions:** Tiramisu RPC edge, 2026-10-09 12:09:17 UTC; Python 3.12.10 urllib.
- **Suggested fix:** exempt JSON-RPC POSTs to the RPC host from the browser-signature rule, or state on the JSON-RPC pages that a custom User-Agent is required.
- **Impact on Door Ledger:** `writer/arkiv.py` sends `User-Agent: door-ledger/0.1` on every call, and the probe sends its own.

### 7. One block parameter, two encodings, Rust debug text in errors

- **Surface:** the block argument of `arkiv_query`, `arkiv_getEntity` and `arkiv_getEntityCount`.
- **Expected:** `json-rpc/querying-data.mdx` documents each method accurately: `arkiv_getEntity` takes a block number as parameter 1 and has no `select` (lines 44-46), `arkiv_query` takes `atBlock` as a hex string (line 140), `arkiv_getEntityCount` takes `block` as a number (line 294). The friction is the inconsistency, not a docs error.
- **Actual:** at block 348996 (`0x55344`):

  | Request | Result |
  |---|---|
  | `arkiv_getEntityCount [{"query":"$owner = addr(0x...dead)","block":348996}]` | `1` |
  | same with `"block":"0x55344"` | -32602 `invalid params: ErrorObject { code: InvalidParams, message: "Invalid params", data: Some(RawValue("invalid type: string \"0x55344\", expected u64 at line 1 column 89")) }` |
  | `arkiv_getEntity ["0x7281...d927", 348996]` | the entity |
  | `arkiv_getEntity ["0x7281...d927", "0x55344"]` | -32602 `invalid block param: ErrorObject { ... "invalid type: string \"0x55344\", expected u64 at line 1 column 10" ... }` |
  | `arkiv_getEntity ["0x7281...d927", {"select":{"owner":true}}]` | -32602 `invalid block param: ErrorObject { ... "invalid type: map, expected u64 at line 1 column 1" ... }` |
  | `arkiv_query ["$key = key(0x7281...d927)", {"atBlock":348996,"limit":1}]` | -32602 ``invalid options param: ErrorObject { ... "invalid type: integer `348996`, expected a string at line 1 column 19" ... }`` |

  `arkiv_query` returns `blockNumber` as hex, so pinning a count or a single-entity read to the block of a query result needs a conversion each way. `limit` already accepts both forms (the probe sends it as a JSON number and as `"0x1"`). The -32602 messages carry a Rust `Debug` dump rather than a sentence.
- **Steps to reproduce:** the six requests in the table, with `0x7281...d927` being the Door Ledger self-test entity (full key in `arkiv/evidence/burn-check-2026-10-09.json`; it lives until block 952018) and any current block number.
- **Versions:** node reth/v2.5.0-189c0df on Tiramisu 7738577, block 348996, 2026-10-09 12:08:50 UTC; docs 45099f7.
- **Suggested fix:** accept both a JSON number and a hex quantity for every block parameter, as `limit` already does, and return the inner message as the error text (for example "block: expected a number, got the string 0x55344").
- **Impact on Door Ledger:** the reader keeps blocks as numbers, sends `atBlock` as `"0x" + hex` and passes the plain number to `arkiv_getEntityCount` (`web/app.js`, `opts` and `loadHistory`).

### 8. Unix seconds in `expiresAt` accepted without a bound

- **Surface:** `expiresAt` in create and extend.
- **Expected:** `json-rpc/mutating-entities.mdx:60` and `:164-178` say `expiresAt` is an absolute block height (or 0), resolved together with `minLifetime`, and that durations convert at 2 s per block. The unit is documented clearly; the docs are silent on any maximum.
- **Actual:** at head 348998, a create with `expiresAt` = 1791634134 (the unix time one day ahead) simulated fine at 85,920 gas, the same estimate as the 100-block create in item 5. It is 1,791,285,136 blocks ahead, about 113.5 years at 2 s per block. Nothing warns.
- **Steps to reproduce:** `eth_estimateGas` from `0x...beef` of `execute([create(salt 0xa3, expiresAt <now in unix seconds + 86400>, minLifetime 0, flags 0, [kind: str "x"])])`.
- **Versions:** node reth/v2.5.0-189c0df on Tiramisu 7738577, block 348998, 2026-10-09 12:08:54 UTC; docs 45099f7; eth_abi 6.0.0, no SDK.
- **Suggested fix:** state whether there is a maximum lifetime. Reject, or require an explicit opt-in for, an `expiresAt` further ahead than some sane horizon, which also catches values that are plainly unix seconds.
- **Impact on Door Ledger:** the writer always sends `expiresAt = 0` and a `minLifetime` in blocks (leases 900 or 7,200, episodes 7,776,000 = 180 days; `writer/engine.py`, `writer/run.py`), so a seconds-for-blocks mix-up cannot reach the chain from our code.

### 9. `$updatedAt`: two docs pages disagree

- **Surface:** system attributes in queries.
- **Expected:** `start-here/fundamentals.mdx:103` lists `$updatedAt` among the system attributes you can query. `json-rpc/querying-data.mdx:125` lists it as not filterable, returned by projections only.
- **Actual:** `$updatedAt >= u64(0)` gives -32002 `"$updatedAt is not queryable \u2014 it is returned by projections only"`. The JSON-RPC page is right.
- **Steps to reproduce:**
  ```json
  {"jsonrpc":"2.0","id":1,"method":"arkiv_query","params":["$updatedAt >= u64(0)",{"limit":1}]}
  ```
- **Versions:** node reth/v2.5.0-189c0df on Tiramisu 7738577, block 348996, 2026-10-09 12:08:53 UTC; docs 45099f7.
- **Suggested fix:** remove `$updatedAt` from the queryable list in `start-here/fundamentals.mdx:103`.
- **Impact on Door Ledger:** none; we do not filter on `$updatedAt`.

### 10. An expired entity can still be deleted

- **Surface:** delete after expiry.
- **Expected:** `start-here/fundamentals.mdx:147` says that from the expiration block on, the entity is gone from every query and rejects every operation.
- **Actual:** entity `0x230b...694b` (owner `0x679b5ab1cd4b488d73025d0d72d4017fa2d97b59`) expired at block 348989 (see item 12). At head 348999, simulated from its owner, `execute([delete])` succeeded (24,849 gas) while `execute([extend minLifetime 100])` reverted `EntityExpired(0x230b...694b, 348989)`. In source, delete checks only that the entity exists and who owns it (`crates/arkiv-reth-executor/src/arkiv.rs:290-300`). Each applied operation emits one event (`json-rpc/mutating-entities.mdx:35`), so a delete like this, if sent, would put an `EntityDeleted` on the stream for an entity readers already treat as gone.
- **Steps to reproduce:** find an entity that expired a few blocks ago and was not deleted (item 12's steps), then `eth_estimateGas` with `from` set to its owner of `execute([delete(<KEY>)])` and of `execute([extend(<KEY>, expiresAt 0, minLifetime 100)])`.
- **Versions:** node reth/v2.5.0-189c0df on Tiramisu 7738577, block 348999, 2026-10-09 12:09:08 UTC; docs 45099f7; eth_abi 6.0.0, no SDK.
- **Suggested fix:** revert delete on an expired entity with `EntityExpired`, or document that delete still works after expiry (for example as a cleanup path) and that it emits `EntityDeleted`.
- **Impact on Door Ledger:** low. The reader treats `expiresAt <= head` as gone whatever the event stream says, and re-reads a row by key on any event (`web/app.js`).

### 11. Extending to the same expiry is accepted

- **Surface:** the Extend Expiry operation.
- **Expected:** `json-rpc/mutating-entities.mdx:96` says the new expiry must be later than the current one, and its error table (line 424) describes `ExpiryNotExtended` as a new expiry not later than the current one. `typescript-sdk/mutating-data.mdx:330` says the new expiry must not be earlier. The two pages disagree on the equal case.
- **Actual:** `execute([create(expiresAt 349998, minLifetime 0), extend(same key, expiresAt 349998, minLifetime 0)])` from `0x...beef` at head 348998 simulated fine (95,960 gas). The node follows the TypeScript page; in source an equal expiry is an accepted no-op (`crates/arkiv-reth-executor/src/arkiv.rs:243`).
- **Steps to reproduce:** read `entityNonce(0x...beef)` with `eth_call` (selector `0x36917bfd`), predict the key for salt `0xa4` (item 16), then `eth_estimateGas` from `0x...beef` of `execute([create(salt 0xa4, expiresAt H+1000, minLifetime 0, flags 0, [kind: str "x"]), extend(<predicted key>, expiresAt H+1000, minLifetime 0)])` where H is the current head.
- **Versions:** node reth/v2.5.0-189c0df on Tiramisu 7738577, block 348998, 2026-10-09 12:08:55 UTC; docs 45099f7; eth_abi 6.0.0, no SDK.
- **Suggested fix:** align `json-rpc/mutating-entities.mdx` with the TypeScript page and the node (an equal expiry is allowed), and say whether an equal extend emits `ExpiryExtended`.
- **Impact on Door Ledger:** none in practice. Our heartbeat extends with a `minLifetime` counted from the current block, which always lands later than the current expiry (`writer/engine.py`).

---

## Worked as documented

### 12. Expiry emits no event; reads hide the entity at `expiresAt`

- **Surface:** expiry, `arkiv_getEntity` history, `eth_getLogs`.
- **Expected:** `start-here/fundamentals.mdx:147` and `typescript-sdk/live-events.mdx:81`: no event fires when an entity expires; an off-chain mirror has to track `$expiresAt` itself.
- **Actual:** `$expiresAt <= u64(348989)` at `atBlock` 348699 returned 5 entities that were live then. The one expiring last, `0x230b...694b`, was created at block 348089 with a 900-block life. `arkiv_getEntity` at 348988 returned it; at 348989 and at the head it returned `null`. `eth_getLogs` for that key from 348089 to 348999 returned a single `EntityCreated` at 348089 and nothing at or after the expiry block.
- **Steps to reproduce:**
  ```json
  {"jsonrpc":"2.0","id":1,"method":"arkiv_query","params":["$expiresAt <= u64(<H-10>)",{"atBlock":"<hex of H-300>","limit":5,"select":{"key":true,"owner":true,"createdAt":true,"expiresAt":true}}]}
  {"jsonrpc":"2.0","id":2,"method":"eth_getLogs","params":[{"address":"0x4400000000000000000000000000000000000044","topics":[null,["<KEY>"]],"fromBlock":"<createdAt>","toBlock":"<hex of H>"}]}
  {"jsonrpc":"2.0","id":3,"method":"arkiv_getEntity","params":["<KEY>",<expiresAt - 1>]}
  {"jsonrpc":"2.0","id":4,"method":"arkiv_getEntity","params":["<KEY>",<expiresAt>]}
  ```
- **Versions:** node reth/v2.5.0-189c0df on Tiramisu 7738577, blocks 348699 to 348999, 2026-10-09 12:08:56 UTC; docs 45099f7.
- **Impact on Door Ledger:** a watcher lease that stops being extended must vanish from "Open now" on its own, with no event to say so. The reader ages rows client-side: on every `newHeads` it drops leases and pulses with `expiresAt <= head` (`web/app.js`). The writer tracks each lease's `expiresAt` locally and re-leases a halt it still sees (`writer/engine.py`).

### 13. Read-only blocks patch, not delete

- **Surface:** creation flag bit 0.
- **Expected:** `json-rpc/mutating-entities.mdx:186`: with read-only set, attributes and payload never change; extend, transfer and delete stay allowed.
- **Actual:** `entityNonce(0x...beef)` = 0. `execute([create(flags 1), delete(predicted key)])` simulated fine (95,960 gas). `execute([create(flags 1), patch(predicted key, [kind: str "y"])])` reverted `ReadOnlyEntity(0x4134...2673)`.
- **Steps to reproduce:** `eth_call` `entityNonce(0x...beef)`, predict the key for salt `0xa5`, then `eth_estimateGas` from `0x...beef` of the two batches above.
- **Versions:** node reth/v2.5.0-189c0df on Tiramisu 7738577, block 348999, 2026-10-09 12:09:09 UTC; docs 45099f7; eth_abi 6.0.0, no SDK.
- **Impact on Door Ledger:** read-only alone does not make a record permanent, because its owner can still delete it. A closed episode is therefore created with flags 3 (read-only plus permissionless extension) and transferred to `0x000000000000000000000000000000000000dEaD` in the same batch (`writer/engine.py`). After that the creator's delete and transfer revert `NotOwner` (`arkiv/evidence/burn-check-2026-10-09.json`).

### 14. Permissionless extension by a third party

- **Surface:** creation flag bit 1 and the `ExpiryExtended` event.
- **Expected:** `json-rpc/mutating-entities.mdx:187`: with this flag any account can extend the expiry. `typescript-sdk/live-events.mdx:124`: the event's `owner` is the entity's owner, not necessarily the account that extended it. One docs inconsistency: `start-here/fundamentals.mdx:111` lists `extendEntity()` as owner-only.
- **Actual:** transaction https://tiramisu.explorer.arkiv.network/tx/0x6b51f07f4279d1a9a692495a130d7937ddef9bff7089d8e0ffda699deb963ea1 (block 347218, status 1) was sent by `0x730e78fc5afb38689fd6b2730dc1e8b59911f71e`, which neither created nor owns entity `0x7281...d927` (creator `0x65ebe97db5cd7160bf9a2aa7818241f9e5768a92`, owner `0x...dEaD`). Its `ExpiryExtended` log has owner topic `0x000000000000000000000000000000000000dead` and new expiry 952018. The extender shows up only as the transaction's `from`.
- **Steps to reproduce:**
  ```json
  {"jsonrpc":"2.0","id":1,"method":"eth_getTransactionByHash","params":["<tx hash above>"]}
  {"jsonrpc":"2.0","id":2,"method":"eth_getTransactionReceipt","params":["<tx hash above>"]}
  ```
- **Versions:** node reth/v2.5.0-189c0df on Tiramisu 7738577, tx in block 347218, read 2026-10-09 12:09:11 UTC; docs 45099f7.
- **Suggested fix (docs only):** in `start-here/fundamentals.mdx:111`, say "owner, or any account if the entity has permissionless extension".
- **Impact on Door Ledger:** anyone can keep a closed episode alive, which is what we want from a public record. A page that wants to credit who extended must read `tx.from`. Our live WebSocket filter therefore includes `0x...dEaD` among the owner topics, so the page also hears extensions of burned episodes.

### 15. `execute()` calldata from eth_abi equals the docs example

- **Surface:** the raw write format.
- **Expected:** `json-rpc/mutating-entities.mdx:244-318` walks through one create (salt `0x1234`, 30 days = 1,296,000 blocks, `category` as str, `count` as u64 100, a JSON payload) and prints the full calldata.
- **Actual:** `writer/arkiv.py` (eth_abi 6.0.0) produces the same 1,188-byte calldata, byte for byte (sha256 `46f31626...2700a9`). Offline check, no RPC.
- **Steps to reproduce:** `python tools/friction_probe.py --only calldata_matches_docs`, which encodes the example with `writer/arkiv.py` and compares it with the docs' calldata embedded in the probe.
- **Versions:** eth_abi 6.0.0, Python 3.12.10, 2026-10-09 12:09:12 UTC; docs 45099f7.
- **Impact on Door Ledger:** this is what let us skip the SDK and keep the writer to two small Python dependencies.

### 16. Entity key prediction

- **Surface:** entity key derivation and `entityNonce(address)`.
- **Expected:** `json-rpc/mutating-entities.mdx:190-226`: the key is keccak256 over chain id, the Arkiv address, the owner, the owner's entity nonce and the salt; the nonce is read with `eth_call` (selector `0x36917bfd`).
- **Actual:** for https://tiramisu.explorer.arkiv.network/tx/0x744ddd5e8089f47c1025956cd3b547928b4591a681f22ab85276cfa670988c59 (block 347215), the salt decoded from the transaction input is `0x91e25ba82feac4fac064c920223a2836`, `entityNonce(sender)` read at block 347214 is 0, and the predicted key `0x7281...d927` equals the key in the receipt's `EntityCreated` log.
- **Steps to reproduce:** `eth_getTransactionByHash` and `eth_getTransactionReceipt` for that hash, decode the create's salt from `input`, and
  ```json
  {"jsonrpc":"2.0","id":1,"method":"eth_call","params":[{"to":"0x4400000000000000000000000000000000000044","data":"0x36917bfd00000000000000000000000065ebe97db5cd7160bf9a2aa7818241f9e5768a92"},"0x54c4e"]}
  ```
- **Versions:** node reth/v2.5.0-189c0df on Tiramisu 7738577, tx in block 347215, read 2026-10-09 12:09:12 UTC; docs 45099f7.
- **Impact on Door Ledger:** the writer predicts keys so that one atomic batch can create an episode and transfer that same new entity to `0x...dEaD` (`writer/engine.py`).

### 17. Browser access: CORS and WebSocket subscriptions

- **Surface:** using the public RPC from a static web page.
- **Expected:** `start-here/fundamentals.mdx:166` calls the read-only client safe for frontend use, and `networks/tiramisu.mdx:34` lists the WebSocket endpoint. Docs silent on CORS.
- **Actual:** a CORS preflight with `Origin: https://example.github.io` got 204 with `access-control-allow-origin: *`, `access-control-allow-methods: POST, OPTIONS` and `access-control-allow-headers: Content-Type, Authorization, X-Api-Key`. Over `wss://rpc.tiramisu.db-chain.testnet.arkiv.network` with the same `Origin` header, `eth_subscribe` for `newHeads` and for `logs` on `0x44...44` both returned subscription ids, and 1 head and 2 logs arrived within 3 s. Caveat: the `Origin` header came from a Python client (websockets 15.0.1), not a real browser.
- **Steps to reproduce:**
  ```json
  {"jsonrpc":"2.0","id":1,"method":"eth_subscribe","params":["newHeads"]}
  {"jsonrpc":"2.0","id":2,"method":"eth_subscribe","params":["logs",{"address":"0x4400000000000000000000000000000000000044"}]}
  ```
  sent over the WebSocket URL; plus an HTTP `OPTIONS` to the RPC URL with `Origin`, `Access-Control-Request-Method: POST` and `Access-Control-Request-Headers: content-type`.
- **Versions:** Tiramisu RPC and WSS endpoints, block 348999, 2026-10-09 12:09:14 UTC; websockets 15.0.1.
- **Impact on Door Ledger:** the reader has no backend. It calls the RPC straight from the browser and follows `newHeads` and registry logs over the WebSocket (`web/app.js`, `connect`).

---

## Not included

Things we noticed earlier that this probe cannot re-run from JSON-RPC (for example how the block explorer and the data explorer display some entities) are left out. So is anything we read in the node source but did not observe on the live node, apart from the source lines quoted above to explain an observed result.
