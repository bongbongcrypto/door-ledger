// Door Ledger reader configuration. Everything the page trusts is in this file.
// The approved reporter list is the trust root: rows are kept or dropped by their on-chain $creator,
// which the chain sets and nobody can write. The same list is in arkiv/reporters.json.
window.DOOR_LEDGER_CONFIG = {
  rpc: "https://rpc.tiramisu.db-chain.testnet.arkiv.network",
  wss: "wss://rpc.tiramisu.db-chain.testnet.arkiv.network",
  chainId: 7738577,
  registry: "0x4400000000000000000000000000000000000044",
  burn: "0x000000000000000000000000000000000000dead",
  blockSeconds: 2,
  explorer: "https://tiramisu.explorer.arkiv.network",
  dataExplorer: "https://data.arkiv.network",
  repo: "https://github.com/bongbongcrypto/door-ledger",
  reporters: [
    { address: "0x65ebe97db5cd7160bf9a2aa7818241f9e5768a92", label: "Watcher A", host: "always-on server", approved: true },
    { address: "0xc10547dbac4e57b89f0f186d79c3a70b1fe533fb", label: "Watcher B", host: "scheduled job", approved: true },
    { address: "0x730e78fc5afb38689fd6b2730dc1e8b59911f71e", label: "Planted spoof", host: "demo only", approved: false },
  ],
  venues: ["binance", "gate", "kucoin", "bithumb"],
  historyPageSize: 20,
};
