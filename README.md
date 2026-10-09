# Door Ledger

**A public, tamper-proof history of when centralized exchanges shut deposits or withdrawals on a network, written to [Arkiv](https://arkiv.network) by independent watchers and read straight from the chain.**

- Live page: https://bongbongcrypto.github.io/door-ledger/ (static, talks only to the public Arkiv node)
- Demo video (3 min): _link added at submission_
- Chain: Arkiv Tiramisu testnet, chain id 7738577
- Built for the Arkiv Global Tour Stop (9 to 18 October 2026). Tracks: Censorship Resistance, Open Source, Security.

## Check it in two minutes

1. Open the live page. "Open now" lists network-wide halts that a watcher is confirming right now. Each row says which watchers see it and when its lease lapses if nobody renews it.
2. Look at "Watchers": every watcher posts an hourly pulse per venue, so an empty "Open now" never just means a dead watcher.
3. Turn off "Approved watchers only". A planted fake halt (Binance, ETH, withdrawals) appears, tagged "unapproved creator". It has exactly the same attributes as a real row; only its on-chain `$creator` gives it away.
4. Scroll to "Verify it yourself" and paste the curl command into a terminal. You get the same rows from the same public node, pinned to the same block.
5. Open any closed record in the ledger: its owner is `0x...dEaD`. The watcher that wrote it can no longer delete or edit it (`python tools/burn_check.py --dry-run`, live evidence in `arkiv/evidence/burn-check-2026-10-09.json`).

## The problem

Exchanges switch deposits and withdrawals off per network all the time: wallet maintenance, a chain upgrade, an incident. While a door is shut, people who need to move funds are stuck, and arbitrage between venues breaks. Afterwards there is no independent record of it.

- The exchanges' own endpoints only say whether a door is open now. None of the four we read (Binance, Gate, KuCoin, Bithumb) says when it last changed. We checked: Binance's `updateTime` does not move when a door flips.
- So a halt's start, end and length exist only if someone outside the exchange writes them down, and if that someone runs a normal server, the history is only as durable and honest as that server.

How often it happens: before this project, the author's own private exchange monitor (a different program with a stricter filter; none of its code is used here) logged 19 network-wide closures that ended between 29 September and 8 October 2026. Six lasted 6 hours or more. The longest was Binance withdrawals on Polkadot Asset Hub, shut for about 105 hours. Bithumb also stopped every coin for about 10 hours on 30 September. These numbers motivate the project; they were not written to Arkiv.

## How it works

```
 exchange public endpoints            watchers (open source, anyone can run one)           Arkiv Tiramisu
 ------------------------             ------------------------------------------           --------------
 Binance  getNetworkCoinAll   --->    Watcher A  always-on server, polls every 120 s  --->  lease    (open halt, short life, renewed)
 Gate     /spot/currencies    --->    Watcher B  GitHub Actions, every 30 min         --->  episode  (closed halt, read-only, burned)
 KuCoin   /currencies         --->                                                    --->  pulse    (hourly, per venue)
 Bithumb  /assetsstatus       --->
                                                                                              ^
                                       static page (this repo, GitHub Pages) ---- queries ----+  no backend
```

- A watcher calls a network halted when at least 3 tradable assets are listed on it and at least 80 % of them are shut, after 2 polls in a row. It calls it reopened at 40 % or less, after 3 polls in a row. Aliases (the same contract open on another route of the same venue) are not halts. Details: `arkiv/schema.md`.
- An open halt is a **lease**: a short-lived entity its watcher keeps extending while it still sees the halt. If the watcher dies, the lease lapses and the halt leaves "Open now" on its own.
- When the network reopens, the watcher writes an **episode** and, in the same atomic batch, transfers it to `0x...dEaD` and deletes the lease. The episode is read-only, lives 180 days, and anyone can extend it.
- Every hour each watcher writes a **pulse** per venue: how many reads succeeded, what it judged, a hash of the response it read.

## Why Arkiv

What would break without it:

| Built on | What fails |
|---|---|
| Our own database or API | We decide what history exists and can quietly edit a duration. If our server stops, readers lose everything. |
| IPFS | Content-addressed files: "Binance halts that lasted 6 h or more since 1 October" needs an indexer we would run, and nothing expires on its own. |
| A subgraph | Needs a contract plus an indexer operator; "a halt that disappears when its watcher dies" needs custom contract logic and keepers. |

What Arkiv gives us, each visible on the page:

| Arkiv feature | Where you see it |
|---|---|
| Typed attributes and range queries on the node | Ledger filters (`dur_s >= u64(21600)`, date range, venue, side) run as plain `arkiv_query` calls from the browser |
| `$creator` set by the protocol | "Approved watchers only" hides the planted spoof; nothing in the row's own data is trusted |
| Per-entity expiry and `extend` | An open halt stays in "Open now" only while its watcher renews it ("lapses in 28 min unless renewed") |
| Permissionless extension flag | Anyone can keep a closed record alive past 180 days |
| Read-only flag plus ownership transfer | Closed records belong to `0x...dEaD`: no one can edit or delete them |
| Atomic batches | Closing a halt (create episode, burn it, delete the lease) lands all at once or not at all |
| `atBlock` | "View the ledger as of block N", and every curl on the page is pinned to the block it was answered at |
| WebSocket logs | New and closed halts appear without a refresh loop |

What stays off Arkiv on purpose: prices and order books, the raw exchange responses (only their sha256 goes on chain), per-coin switch flips that never add up to a network halt, alert logic, and any personal data.

The honest limit: the record is as permanent as the Tiramisu testnet, and trust in a row is trust in the watcher that wrote it. That is why there are two watchers on different hosts, why their code is public, and why anyone can run a third.

## Run your own watcher

Python 3.11 or newer.

```
python -m pip install -r writer/requirements.txt
python -m writer.run --dry-run                    # read the four venues and print what the rule sees; signs nothing
```

To write, put a Tiramisu testnet key in a file outside the repo (one line `ARKIV_SIGNER_HEX=0x...`), fund the address from the [Arkiv faucet](https://hub.arkiv.network/faucet), then:

```
ARKIV_KEY_FILE=/path/to/key.env python -m writer.run --role mywatcher --loop     # long-running, 30-min leases
ARKIV_KEY_FILE=/path/to/key.env python -m writer.run --role mywatcher --once     # one scheduled run, 4-hour leases
```

A watcher costs about 0.015 GLM a day. To have the page show it, add its address to `web/config.js` and `arkiv/reporters.json`.

Serve the page locally with `python -m http.server 8000 --directory web`.

## Tests and checks

```
python -m unittest discover -s tests       # 121 tests: parsers on recorded responses, the halt rule, the engine against a simulated registry
python tools/check_schema.py               # every attribute and query is documented in arkiv/schema.md
python tools/friction_probe.py             # re-runs every item in arkiv/friction.md against the live node (read-only)
python tools/burn_check.py --dry-run       # the burn lock, simulated
```

## Repository map

| Path | What |
|---|---|
| `writer/arkiv.py` | Arkiv over plain JSON-RPC: `execute()` encoding, key prediction, signing, receipts |
| `writer/venues.py` | The four exchange readers and the halt rule |
| `writer/engine.py` | Leases, episodes, pulses; safe retries |
| `writer/run.py` | Command line (`--loop`, `--once`, `--dry-run`) |
| `web/` | The static page (no build step, no dependencies) |
| `arkiv/schema.md` | Entity types, attributes, expiry, the queries the page runs |
| `arkiv/friction.md` | What got in our way, reproduced with `tools/friction_probe.py` |
| `arkiv/reporters.json` | The approved watcher list |
| `arkiv/evidence/` | Live evidence: burn check, planted spoof, friction probe output |
| `deploy/`, `.github/workflows/` | How Watcher A (systemd) and Watcher B (scheduled Actions job) run |

## Prior work

Everything in this repository was written from 9 October 2026, 11:00 UTC. Before the opening we read the Arkiv docs, ran read-only and simulated calls against Tiramisu, and drafted the data design; none of that code is in this repository. The author also runs a private exchange monitor; it informed which public endpoints to read and supplied the motivating numbers above, and none of its code is reused.

## License

MIT, see `LICENSE`.
