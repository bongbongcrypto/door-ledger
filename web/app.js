// Door Ledger reader: queries the public Arkiv node directly, renders with textContent only
// (payloads are untrusted chain data), and follows new writes over a WebSocket.
(function () {
  "use strict";
  const C = window.DOOR_LEDGER_CONFIG;
  const $ = (id) => document.getElementById(id);
  // A reader can trust a different set of watchers without asking us: ?watchers=0xabc...,0xdef...
  const OWN = (new URLSearchParams(location.search).get("watchers") || "").toLowerCase().split(",")
    .map((x) => x.trim()).filter((x) => /^0x[0-9a-f]{40}$/.test(x));
  const KNOWN = new Map(C.reporters.map((r) => [r.address, OWN.length ? { ...r, approved: OWN.includes(r.address) } : r]));
  for (const a of OWN) if (!KNOWN.has(a)) KNOWN.set(a, { address: a, label: a.slice(0, 6) + "…" + a.slice(-4), approved: true });
  const APPROVED = OWN.length ? OWN : C.reporters.filter((r) => r.approved).map((r) => r.address);
  const SELECT = { key: true, creator: true, owner: true, createdAt: true, expiresAt: true, attributes: true, payload: true };
  const T_CREATED = "0xb282d7c494b8899aa8015cd07be621530beb03409eb8c5e8fdc1411ba64356a5";

  const state = {
    approvedOnly: true,
    atBlock: null,          // null = live
    head: 0,
    headTime: 0,            // unix seconds of `head` (from the node)
    live: new Map(),        // key -> row (leases and pulses)
    hist: [],               // episode rows, newest first
    histCursor: null,
    histBlock: null,
    liveBlock: null,
    histCount: null,
    filters: { venue: "", side: "", minDur: 0, from: "", to: "" },
    ws: null,
    wsOk: false,
  };

  // ---------------------------------------------------------------------------- rpc
  let rpcId = 0;
  async function rpc(method, params) {
    for (let attempt = 0; attempt < 3; attempt++) {
      const res = await fetch(C.rpc, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ jsonrpc: "2.0", id: ++rpcId, method, params }),
      });
      if (res.status === 429) {
        const wait = Math.min(60, Number(res.headers.get("retry-after")) || 10);
        banner("The public Arkiv node is rate limiting this browser. Retrying in " + wait + " s.");
        await new Promise((r) => setTimeout(r, wait * 1000));
        continue;
      }
      const body = await res.json();
      if (body.error) throw new Error(method + ": " + (body.error.message || body.error.code));
      banner("");
      return body.result;
    }
    throw new Error(method + ": rate limited");
  }

  // ---------------------------------------------------------------------------- queries
  const creatorClause = () => "(" + APPROVED.map((a) => "$creator = addr(" + a + ")").join(" OR ") + ")";
  function liveQuery() {
    let q = "app = str('doorledger') AND (kind = str('lease') OR kind = str('pulse'))";
    if (state.approvedOnly) q += " AND " + creatorClause();
    return q;
  }
  function histQuery() {
    const f = state.filters;
    let q = "app = str('doorledger') AND kind = str('episode')";
    if (f.venue) q += " AND venue = str('" + f.venue + "')";
    if (f.side) q += " AND side = str('" + f.side + "')";
    if (f.minDur > 0) q += " AND dur_s >= u64(" + f.minDur + ")";
    if (f.from) q += " AND t0 >= u64(" + Math.floor(Date.parse(f.from + "T00:00:00Z") / 1000) + ")";
    if (f.to) q += " AND t0 < u64(" + Math.floor(Date.parse(f.to + "T00:00:00Z") / 1000) + ")";
    if (state.approvedOnly) q += " AND " + creatorClause();
    return q;
  }
  function opts(limit, atBlock, cursor) {
    const o = { limit, select: SELECT };
    if (atBlock != null) o.atBlock = "0x" + atBlock.toString(16);
    if (cursor) o.cursor = cursor;
    return o;
  }

  // ---------------------------------------------------------------------------- decoding
  function decode(row) {
    const a = {};
    for (const x of row.attributes || []) a[x.name] = x.type === "u64" ? Number(BigInt(x.value)) : x.value;
    let p = null;
    if (row.payload) {
      try { p = JSON.parse(new TextDecoder().decode(hexBytes(row.payload))); } catch (e) { p = null; }
    }
    return {
      key: row.key, creator: (row.creator || "").toLowerCase(), owner: (row.owner || "").toLowerCase(),
      createdAt: num(row.createdAt), expiresAt: num(row.expiresAt), a, p,
    };
  }
  const num = (h) => (typeof h === "string" ? parseInt(h, 16) : Number(h || 0));
  function hexBytes(h) {
    const s = h.startsWith("0x") ? h.slice(2) : h;
    const out = new Uint8Array(s.length / 2);
    for (let i = 0; i < out.length; i++) out[i] = parseInt(s.substr(i * 2, 2), 16);
    return out;
  }

  // ---------------------------------------------------------------------------- formatting
  const pad = (n) => String(n).padStart(2, "0");
  function utc(t) {
    if (!t) return "";
    const d = new Date(t * 1000);
    return d.getUTCFullYear() + "-" + pad(d.getUTCMonth() + 1) + "-" + pad(d.getUTCDate()) + " " + pad(d.getUTCHours()) + ":" + pad(d.getUTCMinutes());
  }
  function span(s) {
    s = Math.max(0, Math.round(s));
    if (s < 3600) return Math.max(1, Math.round(s / 60)) + " min";
    if (s < 86400) return (s / 3600).toFixed(s < 36000 ? 1 : 0) + " h";
    return (s / 86400).toFixed(1) + " days";
  }
  const nowS = () => (state.atBlock != null && state.headTime ? state.headTime : Math.floor(Date.now() / 1000));
  const blockAge = (b) => (state.head - b) * C.blockSeconds;
  const short = (a) => (a ? a.slice(0, 6) + "…" + a.slice(-4) : "");
  function who(addr) {
    const r = KNOWN.get(addr);
    return r ? r.label : short(addr);
  }

  function el(tag, attrs, ...kids) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") e.className = v;
      else if (k === "label") e.dataset.label = v;
      else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v);
    }
    for (const k of kids) if (k != null) e.append(k.nodeType ? k : document.createTextNode(String(k)));
    return e;
  }
  const td = (label, ...kids) => el("td", { label }, ...kids);
  function tag(text, cls, title) { return el("span", { class: "tag " + cls, title: title || "" }, text); }
  function creatorTag(addr) {
    const r = KNOWN.get(addr);
    if (r && r.approved) return el("span", { class: "who" }, r.label);
    return el("span", { class: "who" }, short(addr), " ", tag("unapproved creator", "spoof", "Not on the approved watcher list. Hidden while the switch is on."));
  }
  function byWatcher(g) {
    const order = C.reporters.map((r) => r.address);
    return [...g].sort((x, y) => order.indexOf(x.creator) - order.indexOf(y.creator));
  }
  function emptyRow(cols, text) { return el("tr", { class: "empty" }, el("td", { colspan: cols }, text)); }

  // ---------------------------------------------------------------------------- render
  function renderOpen() {
    const body = $("open-body");
    const leases = [...state.live.values()].filter((r) => r.a.kind === "lease" && r.expiresAt > state.head);
    const groups = new Map();
    for (const r of leases) {
      const id = r.a.venue + "|" + r.a.route + "|" + r.a.side;
      if (!groups.has(id)) groups.set(id, []);
      groups.get(id).push(r);
    }
    body.replaceChildren();
    const rows = [...groups.values()].sort((x, y) => Math.min(...x.map((r) => r.a.t0)) - Math.min(...y.map((r) => r.a.t0)));
    for (const g of rows) {
      const first = g.reduce((m, r) => (r.a.t0 < m.a.t0 ? r : m), g[0]);
      const spoofOnly = g.every((r) => !(KNOWN.get(r.creator) || {}).approved);
      const p = first.p || {};
      const tr = el("tr", { class: "row" + (spoofOnly ? " spoofed" : ""), tabindex: "0", "aria-haspopup": "dialog", onclick: () => showDetail(first), onkeydown: (e) => { if (e.key === "Enter") showDetail(first); } },
        td("Venue", cap(first.a.venue)),
        td("Network", el("span", { class: "num" }, first.a.route), el("span", { class: "sub" }, first.a.net)),
        td("Side", tag(first.a.side + " shut", "open")),
        td("First seen shut", el("span", { class: "num" }, utc(first.a.t0)), el("span", { class: "sub" }, "watched shut for " + span(nowS() - first.a.t0))),
        td("Assets shut", p.closed != null && p.listed ? p.closed + " of " + p.listed : "", el("span", { class: "sub" }, (p.assets || []).slice(0, 6).join(", "))),
        td("Seen by", el("span", { class: "who-list" }, ...byWatcher(g).map((r) => el("span", { class: "who-item" }, creatorTag(r.creator))))),
        td("Lapses unless renewed", el("span", { class: "who-list" }, ...byWatcher(g).map((r) => el("span", { class: "who-item num" },
          who(r.creator) + ": " + span((r.expiresAt - state.head) * C.blockSeconds))))));
      body.append(tr);
    }
    if (!rows.length) body.append(emptyRow(7, "No network-wide halt is open right now at the watched venues. The Watchers table shows they are still reading."));
    $("sum-open").textContent = rows.length + (rows.length === 1 ? " halt" : " halts");
  }

  function renderWatchers() {
    const body = $("watch-body");
    const pulses = [...state.live.values()].filter((r) => r.a.kind === "pulse" && r.expiresAt > state.head);
    const latest = new Map();
    for (const r of pulses) {
      const id = r.a.venue + "|" + r.creator;
      if (!latest.has(id) || latest.get(id).createdAt < r.createdAt) latest.set(id, r);
    }
    body.replaceChildren();
    const writers = state.approvedOnly ? APPROVED : [...new Set([...APPROVED, ...pulses.map((r) => r.creator)])];
    const liveCount = new Set();
    for (const venue of C.venues) {
      for (const w of writers) {
        const r = latest.get(venue + "|" + w);
        const p = (r && r.p) || {};
        if (r) liveCount.add(w);
        body.append(el("tr", r ? { class: "row", tabindex: "0", "aria-haspopup": "dialog", onclick: () => showDetail(r), onkeydown: (e) => { if (e.key === "Enter") showDetail(r); } } : {},
          td("Venue", cap(venue)),
          td("Watcher", creatorTag(w)),
          td("Last pulse", r ? el("span", {}, tag("live", "live"), " ", span(blockAge(r.createdAt)) + " ago") : tag("silent", "silent", "No pulse from this watcher for this venue is alive")),
          td("Reads since last pulse", r ? (p.polls_ok || 0) + " ok, " + (p.polls_fail || 0) + " failed" : ""),
          td("Networks judged", r && p.tradable_routes != null ? String(p.tradable_routes) : ""),
          td("Halted at last read", r ? ((p.halted_routes || []).join(", ") || "none") : "")));
      }
    }
    $("sum-watchers").textContent = liveCount.size + " of " + APPROVED.length;
  }

  function renderHistory() {
    const body = $("hist-body");
    body.replaceChildren();
    for (const r of state.hist) {
      const locked = r.owner === C.burn;
      body.append(el("tr", { class: "row" + ((KNOWN.get(r.creator) || {}).approved ? "" : " spoofed"), tabindex: "0", "aria-haspopup": "dialog", onclick: () => showDetail(r), onkeydown: (e) => { if (e.key === "Enter") showDetail(r); } },
        td("Venue", cap(r.a.venue)),
        td("Network", el("span", { class: "num" }, r.a.route), el("span", { class: "sub" }, r.a.net)),
        td("Side", tag(r.a.side, "ended")),
        td("Started (UTC)", el("span", { class: "num" }, utc(r.a.t0))),
        td("Reopened (UTC)", el("span", { class: "num" }, utc(r.a.t1))),
        td("Lasted", el("span", { class: "num" }, span(r.a.dur_s))),
        td("Written by", creatorTag(r.creator)),
        td("Record", locked ? tag("locked", "locked", "Owned by 0x…dEaD: no one can edit or delete it") : tag("owner " + short(r.owner), "spoof"))));
    }
    if (!state.hist.length) body.append(emptyRow(8, "No closed halt matches these filters."));
    $("more").hidden = !state.histCursor;
    $("sum-closed").textContent = state.histCount == null ? "" : String(state.histCount);
  }

  function renderRepro() {
    const lq = liveQuery(), hq = histQuery();
    $("q-live").textContent = lq;
    $("q-hist").textContent = hq;
    $("q-live-curl").textContent = curl(lq, opts(200, state.liveBlock));
    $("q-hist-curl").textContent = curl(hq, opts(C.historyPageSize, state.histBlock));
  }
  function curl(q, o) {
    const body = JSON.stringify({ jsonrpc: "2.0", id: 1, method: "arkiv_query", params: [q, o] });
    return "curl -s " + C.rpc + " -H 'content-type: application/json' --data-binary @- <<'EOF'\n" + body + "\nEOF";
  }

  function renderChain() {
    const dot = $("livedot");
    dot.className = "dot" + (state.atBlock != null ? " past" : state.wsOk ? " live" : "");
    $("chaintext").textContent = state.atBlock != null
      ? "Viewing block " + state.atBlock + (state.headTime ? " (" + utc(state.headTime) + " UTC)" : "")
      : "Tiramisu block " + (state.head || "…") + (state.wsOk ? ", live" : "");
  }

  function renderAll() { renderOpen(); renderWatchers(); renderHistory(); renderRepro(); renderChain(); }

  const VENUE_NAMES = { binance: "Binance", gate: "Gate", kucoin: "KuCoin", bithumb: "Bithumb" };
  function cap(s) { return VENUE_NAMES[s] || (s ? s.charAt(0).toUpperCase() + s.slice(1) : ""); }

  // ---------------------------------------------------------------------------- detail
  function showDetail(r) {
    const meta = $("detail-meta");
    meta.replaceChildren();
    const add = (k, v) => meta.append(el("dt", {}, k), el("dd", {}, v));
    add("Kind", r.a.kind);
    add("Entity key", el("a", { href: C.explorer + "/entity/" + r.key, target: "_blank", rel: "noopener" }, r.key));
    add("Created by", el("a", { href: C.explorer + "/address/" + r.creator, target: "_blank", rel: "noopener" }, r.creator + " (" + who(r.creator) + ")"));
    add("Owner", r.owner === C.burn ? r.owner + " (nobody: the record is locked)" : r.owner);
    add("Created at block", String(r.createdAt));
    add("Expires at block", String(r.expiresAt) + (r.expiresAt > state.head ? ", in about " + span((r.expiresAt - state.head) * C.blockSeconds) : ""));
    if (r.p && r.p.lease_tx) add("Opening tx", el("a", { href: C.explorer + "/tx/" + r.p.lease_tx, target: "_blank", rel: "noopener" }, r.p.lease_tx));
    const txCell = el("dd", {}, "looking up…");
    meta.append(el("dt", {}, "Created in tx"), txCell);
    rpc("eth_getLogs", [{ address: C.registry, fromBlock: "0x" + r.createdAt.toString(16), toBlock: "0x" + r.createdAt.toString(16), topics: [T_CREATED, r.key] }])
      .then((logs) => txCell.replaceChildren(logs && logs[0] ? el("a", { href: C.explorer + "/tx/" + logs[0].transactionHash, target: "_blank", rel: "noopener" }, logs[0].transactionHash) : "not found"))
      .catch(() => txCell.replaceChildren("lookup failed"));
    $("detail-h").textContent = r.a.kind === "pulse" ? "Pulse" : r.a.kind === "lease" ? "Open halt" : "Closed halt";
    $("detail-payload").textContent = r.p ? JSON.stringify(r.p, null, 2) : "(no JSON payload)";
    $("detail-query").textContent = curl("$key = key(" + r.key + ")", opts(1, state.atBlock));
    $("detail").showModal();
  }

  // ---------------------------------------------------------------------------- loading
  async function loadHead() {
    if (state.atBlock != null) {
      const b = await rpc("eth_getBlockByNumber", ["0x" + state.atBlock.toString(16), false]);
      state.head = state.atBlock;
      state.headTime = b ? num(b.timestamp) : 0;
    } else {
      state.head = num(await rpc("eth_blockNumber", []));
      state.headTime = 0;
    }
  }
  async function loadLive() {
    const res = await rpc("arkiv_query", [liveQuery(), opts(200, state.atBlock)]);
    state.liveBlock = num(res.blockNumber);
    state.live = new Map((res.data || []).map((r) => [r.key, decode(r)]));
  }
  async function loadHistory(more) {
    const at = more ? state.histBlock : state.atBlock;
    const res = await rpc("arkiv_query", [histQuery(), opts(C.historyPageSize, at, more ? state.histCursor : null)]);
    if (!more) { state.hist = []; state.histBlock = num(res.blockNumber); }
    state.hist.push(...(res.data || []).map(decode));
    state.histCursor = res.cursor || null;
    try {
      state.histCount = await rpc("arkiv_getEntityCount", [{ query: histQuery(), block: state.histBlock }]);
    } catch (e) { state.histCount = null; }
    $("hist-note").textContent = "Showing " + state.hist.length + (state.histCount != null ? " of " + state.histCount : "") + ", answered at block " + state.histBlock + ".";
  }
  async function reload() {
    try {
      await loadHead();
      await Promise.all([loadLive(), loadHistory(false)]);
      renderAll();
    } catch (e) {
      banner("Could not read Arkiv: " + e.message);
    }
  }
  function banner(msg) { const b = $("banner"); b.textContent = msg; b.hidden = !msg; }

  // ---------------------------------------------------------------------------- live updates
  const pad32 = (a) => "0x" + "0".repeat(24) + a.slice(2);
  function connect() {
    if (state.atBlock != null || !("WebSocket" in window)) return;
    let ws;
    try { ws = new WebSocket(C.wss); } catch (e) { return; }
    state.ws = ws;
    const pending = new Set();
    let timer = null;
    ws.onopen = () => {
      ws.send(JSON.stringify({ jsonrpc: "2.0", id: 1, method: "eth_subscribe", params: ["newHeads"] }));
      ws.send(JSON.stringify({ jsonrpc: "2.0", id: 2, method: "eth_subscribe", params: ["logs", { address: C.registry, topics: [null, null, [...KNOWN.keys()].map(pad32)] }] }));
      state.wsOk = true; renderChain();
    };
    ws.onmessage = (m) => {
      let msg; try { msg = JSON.parse(m.data); } catch (e) { return; }
      const res = msg.params && msg.params.result;
      if (!res) return;
      if (res.number) { // new head: age the tables, drop what expired
        state.head = num(res.number);
        renderOpen(); renderWatchers(); renderChain();
        return;
      }
      if (res.topics) {
        const key = res.topics[1];
        if (res.topics[0] === T_CREATED || state.live.has(key)) pending.add(key);
        else if (state.hist.some((r) => r.key === key)) pending.add(key);
        clearTimeout(timer);
        timer = setTimeout(() => refreshKeys(pending), 1500);
      }
    };
    ws.onclose = () => { state.wsOk = false; renderChain(); if (state.atBlock == null) setTimeout(connect, 15000); };
    ws.onerror = () => { try { ws.close(); } catch (e) { /* closed */ } };
  }
  async function refreshKeys(pending) {
    const keys = [...pending]; pending.clear();
    for (const key of keys) {
      let row = null;
      try { row = await rpc("arkiv_getEntity", [key]); } catch (e) { row = null; }
      if (!row) { state.live.delete(key); continue; }
      const r = decode(row);
      if (r.a.app !== "doorledger") continue;
      if (state.approvedOnly && !APPROVED.includes(r.creator)) continue;
      if (r.a.kind === "episode") {
        if (!state.hist.some((h) => h.key === key)) { state.hist.unshift(r); state.histCount = (state.histCount || 0) + 1; }
        else state.hist = state.hist.map((h) => (h.key === key ? r : h));
      } else if (r.a.kind === "lease" || r.a.kind === "pulse") {
        state.live.set(key, r);
      }
    }
    renderOpen(); renderWatchers(); renderHistory();
  }

  // ---------------------------------------------------------------------------- controls
  function bind() {
    $("repo-link").href = C.repo; $("repo-foot").href = C.repo;
    $("reporter-list").replaceChildren(...APPROVED.map((a, i) => el("span", {}, (i ? ", " : "") , el("a", { href: C.explorer + "/address/" + a, target: "_blank", rel: "noopener", class: "addr" }, who(a) + " " + short(a)))));
    $("approved-only").addEventListener("change", (e) => { state.approvedOnly = e.target.checked; reload(); });
    const f = state.filters;
    const onFilter = () => {
      f.venue = $("f-venue").value; f.side = $("f-side").value; f.minDur = Number($("f-dur").value);
      f.from = $("f-from").value; f.to = $("f-to").value;
      loadHistory(false).then(() => { renderHistory(); renderRepro(); }).catch((e) => banner("Could not read Arkiv: " + e.message));
    };
    for (const id of ["f-venue", "f-side", "f-dur", "f-from", "f-to"]) $(id).addEventListener("change", onFilter);
    $("more").addEventListener("click", () => loadHistory(true).then(renderHistory).catch((e) => banner(e.message)));
    $("asof-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const v = $("asof-input").value.trim().replace(/^0x/i, "");
      const n = /^[0-9]+$/.test(v) ? Number(v) : NaN;
      if (!Number.isFinite(n) || n < 1) { banner("Enter a block number, for example " + (state.head - 1800) + " (about an hour ago)."); return; }
      state.atBlock = n; $("asof-clear").hidden = false;
      if (state.ws) state.ws.close();
      reload();
    });
    $("asof-clear").addEventListener("click", () => { state.atBlock = null; $("asof-clear").hidden = true; $("asof-input").value = ""; reload().then(connect); });
    for (const b of document.querySelectorAll(".copy")) {
      b.addEventListener("click", () => {
        const text = $(b.dataset.copy).textContent;
        navigator.clipboard.writeText(text).then(() => { b.textContent = "Copied"; setTimeout(() => (b.textContent = "Copy curl"), 1500); },
          () => { $(b.dataset.copy).closest("details").open = true; });
      });
    }
  }

  bind();
  if (OWN.length) $("trust-note").textContent = "You are trusting your own list of watchers from the address bar: " + OWN.map(short).join(", ") + ". Remove ?watchers= to go back to ours.";
  reload().then(connect);
})();
