/* Trading Tracker frontend — talks to /api/* (TradingView MCP or demo fallback). */
const $ = (s) => document.querySelector(s);
const state = { symbol: null, interval: "1D", chart: "tracker", watchlist: [], tools: [] };

const fmt = (n, d = 2) => n === null || n === undefined || isNaN(n) ? "—" :
  Number(n).toLocaleString("en-US", { minimumFractionDigits: Math.abs(n) < 2 ? Math.max(d, 4) : d, maximumFractionDigits: Math.abs(n) < 2 ? 6 : d });
const money = (n) => (n === null || n === undefined ? "—" : "$" + fmt(n));
const cls = (n) => n >= 0 ? "up" : "down";
const signed = (n) => (n >= 0 ? "+" : "") + fmt(n);

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!r.ok) throw new Error((await r.text()).slice(0, 300));
  return r.json();
}

/* ---------------- MCP status ---------------- */
async function loadStatus() {
  const st = await api("/api/mcp/status");
  const pill = $("#mode-pill");
  if (st.connected) {
    pill.textContent = "● LIVE · MCP"; pill.className = "pill live";
    $("#demo-banner").classList.add("hidden");
    $("#mcp-label").textContent = `${st.serverInfo?.name || "MCP"} · ${st.tools.length} tools`;
  } else {
    pill.textContent = "● DEMO DATA"; pill.className = "pill demo";
    $("#demo-banner").classList.remove("hidden");
    $("#mcp-label").textContent = st.configured ? "MCP configured but not connected" : "MCP not connected";
  }
  state.tools = st.tools || [];
  const sel = $("#tool-select");
  sel.innerHTML = state.tools.length
    ? state.tools.map((t) => `<option value="${t.name}">${t.name}</option>`).join("")
    : `<option value="">(no MCP tools — connect first)</option>`;
  updateToolDesc();
  $("#mcp-status-box").textContent = JSON.stringify(
    { connected: st.connected, mode: st.mode, server: st.serverInfo, tools: state.tools.map(t => t.name), error: st.error, config: st.config }, null, 2);
  return st;
}
function updateToolDesc() {
  const t = state.tools.find((x) => x.name === $("#tool-select").value);
  $("#tool-desc").textContent = t ? t.description || "" : "";
  if (t?.inputSchema?.properties) {
    const tmpl = {};
    for (const k of Object.keys(t.inputSchema.properties)) tmpl[k] = /sym|tick|pair/i.test(k) ? (state.symbol || "NASDAQ:AAPL") : "";
    $("#tool-args").value = JSON.stringify(tmpl, null, 2);
  }
}
$("#tool-select").addEventListener("change", updateToolDesc);
$("#btn-run-tool").addEventListener("click", async () => {
  const box = $("#tool-result"); box.textContent = "Running…";
  try {
    const args = JSON.parse($("#tool-args").value || "{}");
    const r = await api("/api/mcp/call", { method: "POST", body: JSON.stringify({ tool: $("#tool-select").value, arguments: args }) });
    box.textContent = JSON.stringify(r.result, null, 2);
  } catch (e) { box.textContent = "Error: " + e.message; }
});

/* ---------------- MCP modal ---------------- */
const modal = $("#mcp-modal");
$("#btn-connect").onclick = async () => { modal.classList.remove("hidden"); await loadStatus(); };
$("#mcp-close").onclick = () => modal.classList.add("hidden");
modal.addEventListener("click", (e) => { if (e.target === modal) modal.classList.add("hidden"); });
$("#mcp-transport").addEventListener("change", (e) => {
  const stdio = e.target.value === "stdio";
  $("#mcp-url-row").classList.toggle("hidden", stdio);
  $("#mcp-cmd-row").classList.toggle("hidden", !stdio);
  $("#mcp-args-row").classList.toggle("hidden", !stdio);
});
$("#mcp-save").onclick = async () => {
  const box = $("#mcp-status-box"); box.textContent = "Connecting… (handshake + tools/list)";
  let headers = {};
  try { headers = JSON.parse($("#mcp-headers").value || "{}"); }
  catch { box.textContent = "Headers JSON is invalid — fix it or clear the field."; return; }
  try {
    const st = await api("/api/mcp/config", {
      method: "POST",
      body: JSON.stringify({
        transport: $("#mcp-transport").value,
        url: $("#mcp-url").value.trim(),
        command: $("#mcp-command").value.trim(),
        args: $("#mcp-args").value.trim() ? $("#mcp-args").value.trim().split(/\s+/) : [],
        headers,
      }),
    });
    box.textContent = JSON.stringify({ connected: st.connected, server: st.serverInfo, tools: (st.tools || []).map(t => t.name), error: st.error }, null, 2);
    await loadStatus(); await refreshAll();
  } catch (e) { box.textContent = "Error: " + e.message; }
};
$("#mcp-disconnect").onclick = async () => {
  await api("/api/mcp/config", { method: "DELETE" });
  await loadStatus(); await refreshAll();
};

/* ---------------- Watchlist ---------------- */
function spark(canvas, data, up) {
  const ctx = canvas.getContext("2d"); const w = canvas.width = 148, h = canvas.height = 48;
  if (!data?.length) return;
  const min = Math.min(...data), max = Math.max(...data), range = max - min || 1;
  ctx.clearRect(0, 0, w, h); ctx.beginPath();
  data.forEach((v, i) => { const x = i / (data.length - 1) * w, y = h - 4 - (v - min) / range * (h - 8); i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
  ctx.strokeStyle = up ? "#26c281" : "#e34c5c"; ctx.lineWidth = 2; ctx.stroke();
}
async function loadWatchlist() {
  const { watchlist } = await api("/api/watchlist");
  state.watchlist = watchlist;
  if (!state.symbol || !watchlist.includes(state.symbol)) state.symbol = watchlist[0];
  const { quotes } = await api(`/api/quotes?symbols=${encodeURIComponent(watchlist.join(","))}`);
  $("#watchlist").innerHTML = "";
  quotes.forEach((q) => {
    const el = document.createElement("div");
    el.className = "watch-item" + (q.symbol === state.symbol ? " active" : "");
    el.innerHTML = `<div><div class="sym">${q.symbol}</div><div class="muted small">${q.source === "mcp" ? "live" : "demo"}</div></div>
      <canvas></canvas>
      <div style="text-align:right"><div><b>${fmt(q.price)}</b></div><div class="${cls(q.changePercent)} small">${signed(q.changePercent)}%</div></div>
      <button class="rm" title="Remove">✕</button>`;
    spark(el.querySelector("canvas"), q.sparkline, (q.changePercent ?? 0) >= 0);
    el.onclick = () => { state.symbol = q.symbol; loadWatchlist(); loadDetail(); };
    el.querySelector(".rm").onclick = async (e) => {
      e.stopPropagation();
      await api(`/api/watchlist/${encodeURIComponent(q.symbol)}`, { method: "DELETE" });
      if (state.symbol === q.symbol) state.symbol = null;
      loadWatchlist(); loadDetail();
    };
    $("#watchlist").appendChild(el);
  });
}
$("#add-symbol-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const v = $("#add-symbol-input").value.trim().toUpperCase(); if (!v) return;
  await api("/api/watchlist", { method: "POST", body: JSON.stringify({ symbol: v }) });
  $("#add-symbol-input").value = ""; state.symbol = v; loadWatchlist(); loadDetail();
});

/* search with small debounce */
let searchTimer;
$("#search-input").addEventListener("input", (e) => {
  clearTimeout(searchTimer);
  const q = e.target.value.trim(); const box = $("#search-results");
  if (q.length < 1) { box.classList.add("hidden"); return; }
  searchTimer = setTimeout(async () => {
    const { results } = await api(`/api/search?q=${encodeURIComponent(q)}`);
    box.innerHTML = results.map((r) => `<div data-sym="${r.symbol}"><b>${r.symbol}</b> <span class="muted">${r.name || ""} ${r.type ? "· " + r.type : ""}</span></div>`).join("") || `<div class="muted">No results</div>`;
    box.classList.remove("hidden");
    box.querySelectorAll("[data-sym]").forEach((d) => d.onclick = async () => {
      await api("/api/watchlist", { method: "POST", body: JSON.stringify({ symbol: d.dataset.sym }) });
      state.symbol = d.dataset.sym; box.classList.add("hidden"); $("#search-input").value = "";
      loadWatchlist(); loadDetail();
    });
  }, 250);
});
document.addEventListener("click", (e) => { if (!e.target.closest(".searchbox")) $("#search-results").classList.add("hidden"); });

/* ---------------- Detail: quote + candles + technicals ---------------- */
function drawChart(candles) {
  const c = $("#chart"); const dpr = window.devicePixelRatio || 1;
  const W = c.clientWidth, H = 340; c.width = W * dpr; c.height = H * dpr; c.style.height = H + "px";
  const ctx = c.getContext("2d"); ctx.scale(dpr, dpr);
  if (!candles?.length) { ctx.fillStyle = "#8b95ad"; ctx.fillText("No candle data", 16, 24); return; }
  const volH = 46, priceH = H - volH - 18, padR = 62;
  const highs = candles.map((x) => x.high), lows = candles.map((x) => x.low);
  let min = Math.min(...lows), max = Math.max(...highs); const pad = (max - min) * 0.06 || 1; min -= pad; max += pad;
  const maxVol = Math.max(...candles.map((x) => x.volume || 0), 1);
  const stepX = (W - padR) / candles.length, bw = Math.max(1, stepX * 0.62);
  const y = (p) => 8 + (max - p) / (max - min) * priceH;
  ctx.strokeStyle = "#1d2740"; ctx.fillStyle = "#66718d"; ctx.font = "10px Inter, sans-serif"; ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i++) { const p = min + (max - min) * i / 4, yy = y(p); ctx.beginPath(); ctx.moveTo(0, yy); ctx.lineTo(W - padR, yy); ctx.stroke(); ctx.fillText(fmt(p), W - padR + 6, yy + 3); }
  candles.forEach((k, i) => {
    const x = i * stepX + stepX / 2, up = k.close >= k.open, col = up ? "#26c281" : "#e34c5c";
    ctx.strokeStyle = col; ctx.beginPath(); ctx.moveTo(x, y(k.high)); ctx.lineTo(x, y(k.low)); ctx.stroke();
    ctx.fillStyle = col; const top = y(Math.max(k.open, k.close)), bot = y(Math.min(k.open, k.close));
    ctx.fillRect(x - bw / 2, top, bw, Math.max(1, bot - top));
    const vh = (k.volume || 0) / maxVol * volH; ctx.globalAlpha = 0.55;
    ctx.fillRect(x - bw / 2, H - vh - 4, bw, vh); ctx.globalAlpha = 1;
  });
  const labelEvery = Math.ceil(candles.length / 6);
  ctx.fillStyle = "#66718d";
  candles.forEach((k, i) => { if (i % labelEvery === 0 && k.time) ctx.fillText(new Date(k.time * 1000).toLocaleDateString("en-GB", { day: "2-digit", month: "short" }), i * stepX, H - volH + 12); });
}
function loadTvWidget() {
  const sym = encodeURIComponent(state.symbol || "NASDAQ:AAPL");
  $("#tv-widget").innerHTML = `<iframe src="https://www.tradingview.com/widgetembed/?symbol=${sym}&interval=D&theme=dark&style=1&locale=en&hide_side_toolbar=0&allow_symbol_change=1" allowtransparency="true" scrolling="no" allowfullscreen></iframe>`;
}
async function loadDetail() {
  if (!state.symbol) return;
  const sym = state.symbol;
  const [q, c, t] = await Promise.all([
    api(`/api/quote?symbol=${encodeURIComponent(sym)}`),
    api(`/api/candles?symbol=${encodeURIComponent(sym)}&interval=${state.interval}&limit=120`),
    api(`/api/technicals?symbol=${encodeURIComponent(sym)}&interval=${state.interval}`),
  ]);
  $("#d-symbol").textContent = sym;
  $("#d-price").textContent = fmt(q.price);
  const ch = $("#d-change"); ch.textContent = `${signed(q.change)} (${signed(q.changePercent)}%)`; ch.className = cls(q.change ?? 0);
  $("#d-source").textContent = q.source === "mcp" ? "Source: TradingView MCP (live)" : "Source: demo data — connect MCP for live prices" + (q.mcpError || c.mcpError ? ` · MCP error: ${q.mcpError || c.mcpError}` : "");
  $("#d-ohlc").innerHTML = `Open <b>${fmt(q.open)}</b> · High <b>${fmt(q.high)}</b><br>Low <b>${fmt(q.low)}</b> · Prev close <b>${fmt(q.previousClose)}</b><br>Volume <b>${q.volume ? Number(q.volume).toLocaleString() : "—"}</b>`;
  if (state.chart === "tracker") drawChart(c.candles); else loadTvWidget();
  // gauge: score -3..+3 (or parse recommendation text from MCP)
  let score = typeof t.score === "number" ? t.score : 0;
  const recText = String(t.recommendation || "").toLowerCase();
  if (recText.includes("strong_buy") || recText.includes("strong buy")) score = 3;
  else if (recText === "buy" || recText.includes("buy") && !recText.includes("sell")) score = Math.max(score, 2);
  else if (recText.includes("strong_sell") || recText.includes("strong sell")) score = -3;
  else if (recText.includes("sell")) score = Math.min(score, -2);
  $("#gauge-needle").style.left = `calc(${((score + 3) / 6) * 100}% - 2px)`;
  $("#tech-rec").textContent = t.recommendation || "—";
  $("#tech-rec").className = score >= 1 ? "up" : score <= -1 ? "down" : "";
  const rows = Object.entries(t.indicators || {}).slice(0, 12)
    .map(([k, v]) => `<tr><td>${k}</td><td>${typeof v === "number" ? fmt(v) : v}</td></tr>`).join("");
  $("#indicators").innerHTML = rows || `<tr><td>No indicators returned</td></tr>`;
}
$("#interval-seg").addEventListener("click", (e) => {
  if (!e.target.dataset.interval) return;
  document.querySelectorAll("#interval-seg button").forEach((b) => b.classList.toggle("active", b === e.target));
  state.interval = e.target.dataset.interval; loadDetail();
});
$("#chart-seg").addEventListener("click", (e) => {
  if (!e.target.dataset.chart) return;
  document.querySelectorAll("#chart-seg button").forEach((b) => b.classList.toggle("active", b === e.target));
  state.chart = e.target.dataset.chart;
  $("#chart").classList.toggle("hidden", state.chart !== "tracker");
  $("#tv-widget").classList.toggle("hidden", state.chart !== "tv");
  loadDetail();
});

/* ---------------- Positions & journal ---------------- */
async function loadPositions() {
  const { positions, totalPnl, totalValue } = await api("/api/positions");
  $("#stat-open-pnl").textContent = money(totalPnl); $("#stat-open-pnl").className = cls(totalPnl);
  $("#stat-pos-value").textContent = money(totalValue);
  $("#positions-empty").classList.toggle("hidden", positions.length > 0);
  $("#positions-body").innerHTML = positions.map((p) => `<tr>
    <td><b>${p.symbol}</b>${p.note ? `<div class="muted small">${p.note}</div>` : ""}</td><td>${p.side}</td><td>${p.quantity}</td>
    <td>${fmt(p.entryPrice)}</td><td>${fmt(p.currentPrice)}</td><td>${money(p.marketValue)}</td>
    <td class="${cls(p.pnl)}"><b>${money(p.pnl)}</b> <span class="small">(${signed(p.pnlPercent)}%)</span></td>
    <td><button class="del" data-del-pos="${p.id}">✕</button></td></tr>`).join("");
  document.querySelectorAll("[data-del-pos]").forEach((b) => b.onclick = async () => { await api(`/api/positions/${b.dataset.delPos}`, { method: "DELETE" }); loadPositions(); });
}
async function loadTrades() {
  const { trades, stats } = await api("/api/trades");
  $("#stat-closed-pnl").textContent = money(stats.totalPnl); $("#stat-closed-pnl").className = cls(stats.totalPnl);
  $("#stat-winrate").textContent = stats.count ? stats.winRate + "%" : "—";
  $("#trades-empty").classList.toggle("hidden", trades.length > 0);
  $("#trades-body").innerHTML = trades.map((t) => `<tr>
    <td><b>${t.symbol}</b>${t.note ? `<div class="muted small">${t.note}</div>` : ""}</td><td>${t.side}</td><td>${t.quantity}</td>
    <td>${fmt(t.entryPrice)}</td><td>${fmt(t.exitPrice)}</td>
    <td class="${cls(t.pnl)}"><b>${money(t.pnl)}</b> <span class="small">(${signed(t.pnlPercent)}%)</span></td>
    <td>${t.strategy || ""}</td><td><button class="del" data-del-trade="${t.id}">✕</button></td></tr>`).join("");
  document.querySelectorAll("[data-del-trade]").forEach((b) => b.onclick = async () => { await api(`/api/trades/${b.dataset.delTrade}`, { method: "DELETE" }); loadTrades(); });
}
function formData(form) {
  const o = {};
  new FormData(form).forEach((v, k) => { o[k] = ["quantity", "entryPrice", "exitPrice"].includes(k) ? parseFloat(v) : v; });
  return o;
}
$("#position-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  await api("/api/positions", { method: "POST", body: JSON.stringify(formData(e.target)) });
  e.target.reset(); loadPositions();
});
$("#trade-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  await api("/api/trades", { method: "POST", body: JSON.stringify(formData(e.target)) });
  e.target.reset(); loadTrades();
});
document.querySelectorAll(".tab").forEach((tab) => tab.onclick = () => {
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
  ["positions", "journal", "tools"].forEach((id) => $("#tab-" + id).classList.toggle("hidden", id !== tab.dataset.tab));
});

/* ---------------- boot ---------------- */
async function refreshAll() { await loadStatus(); await loadWatchlist(); await loadDetail(); await loadPositions(); await loadTrades(); }
$("#btn-refresh").onclick = refreshAll;
window.addEventListener("resize", () => { if (state.chart === "tracker") loadDetail(); });
refreshAll();
setInterval(() => { loadWatchlist(); loadDetail(); loadPositions(); }, 20000);
