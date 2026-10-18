"""
Trading Tracker API — FastAPI backend.

Market data flow:
  TradingView MCP server  --(MCP JSON-RPC)-->  this API  -->  web UI
  If no MCP is connected (or a call fails), responses fall back to clearly
  labelled DEMO data so the tracker is always usable.

Run:  uvicorn backend.app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import demo_data, oauth, store
from .mcp_client import (MCPClient, MCPError, mcp_candles, mcp_quote,
                         mcp_quotes_batch, mcp_search, mcp_technicals)

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

# Official TradingView MCP (streamable HTTP + OAuth 2.1 sign-in).
TRADINGVIEW_MCP_URL = "https://mcp.tradingview.com/mcp"

app = FastAPI(title="Trading Tracker", version="1.0.0")


async def build_client(base_cfg: dict | None = None) -> MCPClient:
    """MCP client from saved config, with any stored OAuth token injected in
    memory only (the token is never written into the MCP config file)."""
    cfg = dict(base_cfg if base_cfg is not None else store.load_mcp_config())
    token = await oauth.get_access_token()
    tokens = oauth.load_tokens()
    if token and (not cfg.get("url") or not tokens.get("mcp_url") or tokens["mcp_url"] == cfg.get("url")):
        cfg.setdefault("transport", "streamable-http")
        cfg.setdefault("url", tokens.get("mcp_url") or "")
        cfg["headers"] = {**(cfg.get("headers") or {}), "Authorization": f"Bearer {token}"}
    return MCPClient(cfg)


client = MCPClient(store.load_mcp_config())


@app.on_event("startup")
async def _startup():
    global client
    client = await build_client()
    if client.configured:
        asyncio.create_task(client.connect())


@app.on_event("shutdown")
async def _shutdown():
    await client.disconnect()


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class MCPConfigIn(BaseModel):
    transport: str = Field("streamable-http", description="streamable-http | sse | stdio")
    url: str = ""
    command: str = ""
    args: list[str] | str = []
    headers: dict[str, str] = {}
    env: dict[str, str] = {}


class ToolCallIn(BaseModel):
    tool: str
    arguments: dict = {}


class SymbolIn(BaseModel):
    symbol: str


class PositionIn(BaseModel):
    symbol: str
    side: str = "long"
    quantity: float
    entryPrice: float
    note: str = ""


class TradeIn(BaseModel):
    symbol: str
    side: str = "long"
    quantity: float
    entryPrice: float
    exitPrice: float
    strategy: str = ""
    note: str = ""


# --------------------------------------------------------------------------- #
# MCP connection management
# --------------------------------------------------------------------------- #
@app.get("/api/health")
async def health():
    return {"ok": True, "mode": "live" if client.connected else "demo"}


@app.get("/api/mcp/status")
async def mcp_status():
    return client.status()


@app.post("/api/mcp/config")
async def mcp_set_config(cfg: MCPConfigIn):
    """Save the TradingView MCP connection and test it immediately."""
    global client
    await client.disconnect()
    raw = cfg.model_dump()
    store.save_mcp_config(raw)
    client = await build_client(raw)
    return await client.connect()


@app.post("/api/mcp/test")
async def mcp_test():
    global client
    client = await build_client()
    return await client.connect()


@app.delete("/api/mcp/config")
async def mcp_clear_config():
    global client
    await client.disconnect()
    store.save_mcp_config(None)
    client = MCPClient({})
    return client.status()


# --------------------------------------------------------------------------- #
# OAuth 2.1 sign-in (for MCP servers like the official TradingView MCP)
# --------------------------------------------------------------------------- #
def _redirect_uri(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc
    return f"{proto}://{host}/oauth/callback"


def _oauth_page(title: str, body: str, ok: bool = True) -> HTMLResponse:
    color = "#26c281" if ok else "#e34c5c"
    return HTMLResponse(
        f"<!doctype html><meta charset='utf-8'><title>{title}</title>"
        "<body style='font-family:system-ui;background:#0b0e14;color:#e8ecf4;display:flex;"
        "align-items:center;justify-content:center;min-height:100vh;margin:0'>"
        "<div style='max-width:560px;background:#121722;border:1px solid #232c42;"
        f"border-top:4px solid {color};border-radius:12px;padding:28px'>"
        f"<h2 style='margin-top:0'>{title}</h2><div style='color:#aeb8cf;line-height:1.6'>{body}</div>"
        "<p><a href='/' style='color:#4f8cff'>← Back to Trading Tracker</a></p></div></body>"
        + ("<script>setTimeout(()=>location.href='/',1800)</script>" if ok else "")
    )


@app.get("/api/oauth/start")
async def oauth_start(request: Request):
    """Begin OAuth sign-in: discover, register, redirect user to authorize."""
    mcp_url = client.url or TRADINGVIEW_MCP_URL
    try:
        url = await oauth.start_flow(mcp_url, _redirect_uri(request),
                                     www_authenticate=client.www_authenticate)
    except oauth.OAuthError as exc:
        return _oauth_page("Sign-in could not start", str(exc), ok=False)
    return RedirectResponse(url, status_code=302)


@app.get("/oauth/callback")
async def oauth_callback(code: str = "", state: str = "", error: str = ""):
    """TradingView redirects here after the user approves access."""
    global client
    if error:
        return _oauth_page("Sign-in cancelled", f"Authorization returned: {error}", ok=False)
    if not code:
        return _oauth_page("Sign-in failed", "No authorization code was returned.", ok=False)
    try:
        await oauth.complete_flow(code, state)
    except oauth.OAuthError as exc:
        return _oauth_page("Sign-in failed", str(exc), ok=False)
    client = await build_client()
    st = await client.connect()
    if st["connected"]:
        tools = ", ".join(t["name"] for t in st["tools"][:12])
        return _oauth_page("✅ Connected to TradingView MCP",
                           f"Signed in successfully. Discovered {len(st['tools'])} tools"
                           + (f" ({tools}…)" if tools else "") + ". Redirecting you back…")
    return _oauth_page("Signed in, but the MCP connection needs attention",
                       f"Token saved, but connecting failed: {st.get('error')}", ok=False)


@app.get("/api/oauth/status")
async def oauth_status():
    return {**oauth.token_status(), "authRequired": client.auth_required}


@app.delete("/api/oauth")
async def oauth_sign_out():
    global client
    await oauth.revoke()
    await client.disconnect()
    client = await build_client()
    return {**oauth.token_status(), "mcp": client.status()}


@app.post("/api/mcp/call")
async def mcp_call(req: ToolCallIn):
    """Call any MCP tool directly (used by the Tool Explorer in the UI)."""
    try:
        result = await client.call_tool(req.tool, req.arguments)
        return {"ok": True, "tool": req.tool, "result": result}
    except MCPError as exc:
        raise HTTPException(502, str(exc))


# --------------------------------------------------------------------------- #
# Market data (MCP first, demo fallback) — with tolerant normalisers so most
# TradingView MCP servers' response shapes work without code changes.
# --------------------------------------------------------------------------- #
def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        if isinstance(d, dict) and d.get(k) is not None:
            return d[k]
    return None


def _num(v: Any) -> Optional[float]:
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except (TypeError, ValueError):
        return None


_PRICE_KEYS = ("price", "close", "last", "last_price", "current_price", "lp", "value")


def _normalise_quote(raw: Any, symbol: str, columns: list[str] | None = None) -> Optional[dict]:
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if not isinstance(raw, dict):
        return None
    # TV screener-style row: {"symbol": ..., "d": [values...]} + column names
    if columns and isinstance(raw.get("d"), list):
        raw = {**raw, **dict(zip(columns, raw["d"]))}
    # Find the dict that actually holds the price fields (may be nested).
    candidates = [raw]
    for key in ("quote", "data", "result", "values"):
        v = raw.get(key)
        if isinstance(v, list) and v:
            v = v[0]
        if isinstance(v, dict):
            candidates.append(v)
            for key2 in ("data", "values"):
                if isinstance(v.get(key2), dict):
                    candidates.append(v[key2])
    inner, price = None, None
    for cand in candidates:
        p = _num(_first(cand, *_PRICE_KEYS))
        if p is not None:
            inner, price = cand, p
            break
    if inner is None or price is None:
        return None
    prev = _num(_first(inner, "previousClose", "prev_close", "previous_close"))
    chg_abs = _num(_first(inner, "change_abs", "changeAbs", "chg_abs"))
    chg_raw = _num(_first(inner, "change", "chg"))
    chg_pct = _num(_first(inner, "changePercent", "change_percent", "chg_percent", "change_pct"))
    if chg_abs is not None:
        # TradingView screener semantics: change = %, change_abs = absolute
        chg = chg_abs
        if chg_pct is None:
            chg_pct = chg_raw
    else:
        chg = chg_raw
    if prev is None and chg is not None:
        prev = price - chg
    if chg is None and prev:
        chg = price - prev
    if chg_pct is None and prev:
        chg_pct = (price - prev) / prev * 100
    sym = _first(raw, "symbol", "s", "ticker") or symbol
    return {
        "symbol": str(sym).upper(), "price": price,
        "open": _num(_first(inner, "open")), "high": _num(_first(inner, "high")),
        "low": _num(_first(inner, "low")), "volume": _num(_first(inner, "volume")),
        "previousClose": prev, "change": chg, "changePercent": chg_pct,
        "name": _first(inner, "description", "name"),
    }


def _normalise_candles(raw: Any) -> Optional[list[dict]]:
    rows: Any = None
    if isinstance(raw, list):
        rows = raw
    elif isinstance(raw, dict):
        rows = _first(raw, "candles", "data", "ohlcv", "bars", "result", "history", "values")
    if not isinstance(rows, list) or not rows:
        return None
    out = []
    for r in rows:
        if isinstance(r, (list, tuple)) and len(r) >= 5:  # [time, o, h, l, c, (v)]
            t, o, h, l, c = r[:5]
            v = r[5] if len(r) > 5 else 0
        elif isinstance(r, dict):
            t = _first(r, "time", "timestamp", "date", "datetime", "t")
            o, h, l, c = (_first(r, "open", "o"), _first(r, "high", "h"),
                          _first(r, "low", "l"), _first(r, "close", "c"))
            v = _first(r, "volume", "v") or 0
        else:
            continue
        o, h, l, c = _num(o), _num(h), _num(l), _num(c)
        if None in (o, h, l, c):
            continue
        ts = _num(t)
        if ts is not None and ts > 1e12:  # ms -> s
            ts /= 1000
        out.append({"time": int(ts) if ts else None, "open": o, "high": h, "low": l, "close": c, "volume": _num(v) or 0})
    out.sort(key=lambda x: x["time"] or 0)
    return out or None


@app.get("/api/quote")
async def quote(symbol: str = Query(...)):
    symbol = symbol.upper()
    if client.connected:
        try:
            raw = await mcp_quote(client, symbol)
            norm = _normalise_quote(raw, symbol)
            if norm:
                return {"source": "mcp", **norm}
            return {"source": "mcp-raw", "symbol": symbol, "raw": raw,
                    "warning": "Could not map MCP quote fields — tell the assistant your MCP's tool/response format to add a mapping."}
        except MCPError as exc:
            return {"source": "demo", "mcpError": str(exc), **demo_data.quote(symbol)}
    return {"source": "demo", **demo_data.quote(symbol)}


@app.get("/api/quotes")
async def quotes(symbols: str = Query("", description="comma-separated")):
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()] or store.get("watchlist")
    # Prefer ONE batch MCP call (TradingView get_symbol_data_batch, up to 50
    # symbols) over N single calls — keeps us well inside MCP rate limits.
    if client.connected and syms:
        try:
            raw = await mcp_quotes_batch(client, syms[:50])
            colnames = None
            rows = raw if isinstance(raw, list) else None
            if isinstance(raw, dict):
                if isinstance(raw.get("columns"), list) and all(isinstance(c, str) for c in raw["columns"]):
                    colnames = raw["columns"]
                rows = _first(raw, "results", "data", "symbols", "rows")
            if isinstance(rows, list):
                found: dict[str, dict] = {}
                for row in rows:
                    sym = str(_first(row, "symbol", "s", "ticker") or "").upper() if isinstance(row, dict) else ""
                    norm = _normalise_quote(row, sym, columns=colnames)
                    if norm:
                        found[norm["symbol"]] = {"source": "mcp", **norm}
                if found:
                    for s in syms:
                        if s not in found:
                            found[s] = await quote(s)  # best-effort fill-in
                    return {"quotes": [found[s] for s in syms]}
        except MCPError:
            pass
    results = await asyncio.gather(*[quote(s) for s in syms], return_exceptions=True)
    out = []
    for s, r in zip(syms, results):
        out.append(r if isinstance(r, dict) else {"source": "demo", **demo_data.quote(s)})
    return {"quotes": out}


@app.get("/api/candles")
async def candles(symbol: str = Query(...), interval: str = "1D", limit: int = 120):
    symbol = symbol.upper()
    if client.connected:
        try:
            raw = await mcp_candles(client, symbol, interval, limit)
            norm = _normalise_candles(raw)
            if norm:
                return {"source": "mcp", "symbol": symbol, "interval": interval, "candles": norm}
            return {"source": "demo", "symbol": symbol, "interval": interval,
                    "candles": demo_data.candles(symbol, interval, limit),
                    "warning": "MCP candle format not recognised; showing demo candles.", "rawType": type(raw).__name__}
        except MCPError as exc:
            return {"source": "demo", "mcpError": str(exc), "symbol": symbol, "interval": interval,
                    "candles": demo_data.candles(symbol, interval, limit)}
    return {"source": "demo", "symbol": symbol, "interval": interval,
            "candles": demo_data.candles(symbol, interval, limit)}


def _nkey(k: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(k).lower())


def _dig(node: Any, names: tuple[str, ...], depth: int = 4) -> Any:
    """Recursively find the first value whose normalised key is in names."""
    if depth < 0 or not isinstance(node, (dict, list)):
        return None
    if isinstance(node, dict):
        for k, v in node.items():
            if _nkey(k) in names and v is not None:
                return v
        for v in node.values():
            found = _dig(v, names, depth - 1)
            if found is not None:
                return found
    else:
        for v in node:
            found = _dig(v, names, depth - 1)
            if found is not None:
                return found
    return None


def _rec_label(value: float) -> tuple[str, int]:
    """TradingView Recommend value in [-1, 1] -> label + gauge score (-3..3)."""
    score = max(-3, min(3, round(value * 3)))
    label = ("Strong Buy" if value >= 0.5 else "Buy" if value >= 0.1 else
             "Strong Sell" if value <= -0.5 else "Sell" if value <= -0.1 else "Neutral")
    return label, score


@app.get("/api/technicals")
async def technicals(symbol: str = Query(...), interval: str = "1D"):
    symbol = symbol.upper()
    if client.connected:
        try:
            raw = await mcp_technicals(client, symbol, interval)
            base = demo_data.technicals(symbol, interval)  # structure fallback
            if isinstance(raw, dict):
                rec_raw = _dig(raw, ("recommendall", "recommend", "recommendation"))
                rec_val = None
                if isinstance(rec_raw, dict):
                    rec_val = _num(_dig(rec_raw, ("summary", "all", "value")))
                elif _num(rec_raw) is not None:
                    rec_val = _num(rec_raw)
                rec_text = rec_raw if isinstance(rec_raw, str) else _dig(raw, ("signal",))
                if rec_val is not None and -1.0 <= rec_val <= 1.0:
                    recommendation, score = _rec_label(rec_val)
                elif isinstance(rec_text, str) and rec_text:
                    recommendation, score = rec_text, base["score"]
                else:
                    recommendation, score = base["recommendation"], base["score"]
                src = _first(raw, "indicators", "values", "data")
                src = src if isinstance(src, dict) else raw
                indicators: dict[str, Any] = {}
                for k, v in src.items():
                    if _num(v) is not None and "recommend" not in _nkey(k):
                        indicators[str(k)] = _num(v)
                    if len(indicators) >= 14:
                        break
                return {"source": "mcp", "symbol": symbol, "interval": interval,
                        "recommendation": recommendation, "score": score,
                        "indicators": indicators or base["indicators"], "raw": raw}
            return {"source": "mcp", "symbol": symbol, "interval": interval,
                    "recommendation": base["recommendation"], "score": base["score"],
                    "indicators": base["indicators"], "raw": raw}
        except MCPError as exc:
            return {"source": "demo", "mcpError": str(exc), **demo_data.technicals(symbol, interval)}
    return {"source": "demo", **demo_data.technicals(symbol, interval)}


@app.get("/api/search")
async def search(q: str = ""):
    if client.connected:
        try:
            raw = await mcp_search(client, q)
            rows = raw if isinstance(raw, list) else _first(raw, "results", "data", "symbols") if isinstance(raw, dict) else None
            if isinstance(rows, list):
                norm = []
                for r in rows[:20]:
                    if isinstance(r, dict):
                        sym = _first(r, "symbol", "ticker", "id", "name")
                        if sym:
                            norm.append({"symbol": str(sym), "name": str(_first(r, "description", "name", "full_name") or sym),
                                         "type": str(_first(r, "type") or ""), "exchange": str(_first(r, "exchange") or "")})
                if norm:
                    return {"source": "mcp", "results": norm}
            return {"source": "demo", "results": demo_data.search(q)}
        except MCPError:
            pass
    return {"source": "demo", "results": demo_data.search(q)}


# --------------------------------------------------------------------------- #
# Watchlist / Positions / Trade journal
# --------------------------------------------------------------------------- #
@app.get("/api/watchlist")
async def watchlist():
    return {"watchlist": store.get("watchlist")}


@app.post("/api/watchlist")
async def watchlist_add(body: SymbolIn):
    return {"watchlist": store.add_watch(body.symbol)}


@app.delete("/api/watchlist/{symbol:path}")
async def watchlist_remove(symbol: str):
    return {"watchlist": store.remove_watch(symbol)}


def _pnl(position: dict, price: float) -> dict:
    qty, entry = position["quantity"], position["entryPrice"]
    direction = -1 if position.get("side") == "short" else 1
    value = qty * price
    pnl = direction * (price - entry) * qty
    return {"currentPrice": price, "marketValue": round(value, 2),
            "pnl": round(pnl, 2), "pnlPercent": round(direction * (price - entry) / entry * 100, 2) if entry else 0}


@app.get("/api/positions")
async def positions():
    items = store.get("positions")
    enriched = []
    for p in items:
        q = await quote(p["symbol"])
        enriched.append({**p, **_pnl(p, q["price"])})
    total = round(sum(p["pnl"] for p in enriched), 2)
    return {"positions": enriched, "totalPnl": total,
            "totalValue": round(sum(p["marketValue"] for p in enriched), 2)}


@app.post("/api/positions")
async def position_add(body: PositionIn):
    from datetime import datetime, timezone
    item = store.add_item("positions", {**body.model_dump(), "symbol": body.symbol.upper(),
                                        "openedAt": datetime.now(timezone.utc).isoformat()})
    return item


@app.delete("/api/positions/{item_id}")
async def position_delete(item_id: str):
    if not store.delete_item("positions", item_id):
        raise HTTPException(404, "position not found")
    return {"ok": True}


@app.get("/api/trades")
async def trades():
    items = store.get("trades")
    for t in items:
        direction = -1 if t.get("side") == "short" else 1
        t["pnl"] = round(direction * (t["exitPrice"] - t["entryPrice"]) * t["quantity"], 2)
        t["pnlPercent"] = round(direction * (t["exitPrice"] - t["entryPrice"]) / t["entryPrice"] * 100, 2) if t["entryPrice"] else 0
    wins = [t for t in items if t["pnl"] > 0]
    return {"trades": items, "stats": {
        "count": len(items), "totalPnl": round(sum(t["pnl"] for t in items), 2),
        "winRate": round(len(wins) / len(items) * 100, 1) if items else 0,
        "bestTrade": max((t["pnl"] for t in items), default=0),
        "worstTrade": min((t["pnl"] for t in items), default=0),
    }}


@app.post("/api/trades")
async def trade_add(body: TradeIn):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    return store.add_item("trades", {**body.model_dump(), "symbol": body.symbol.upper(),
                                     "openedAt": now, "closedAt": now})


@app.delete("/api/trades/{item_id}")
async def trade_delete(item_id: str):
    if not store.delete_item("trades", item_id):
        raise HTTPException(404, "trade not found")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# Static UI
# --------------------------------------------------------------------------- #
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")
