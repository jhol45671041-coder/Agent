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
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import demo_data, store
from .mcp_client import MCPClient, MCPError, mcp_candles, mcp_quote, mcp_search, mcp_technicals

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "static"

app = FastAPI(title="Trading Tracker", version="1.0.0")
client = MCPClient(store.load_mcp_config())


@app.on_event("startup")
async def _startup():
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
    client = MCPClient(raw)
    return await client.connect()


@app.post("/api/mcp/test")
async def mcp_test():
    return await client.connect()


@app.delete("/api/mcp/config")
async def mcp_clear_config():
    global client
    await client.disconnect()
    store.save_mcp_config(None)
    client = MCPClient({})
    return client.status()


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


def _normalise_quote(raw: Any, symbol: str) -> Optional[dict]:
    if isinstance(raw, list) and raw:
        raw = raw[0]
    if not isinstance(raw, dict):
        return None
    inner = _first(raw, "quote", "data", "result") or raw
    if isinstance(inner, list) and inner:
        inner = inner[0]
    if not isinstance(inner, dict):
        return None
    price = _num(_first(inner, "price", "close", "last", "last_price", "current_price", "lp", "value"))
    if price is None:
        return None
    prev = _num(_first(inner, "previousClose", "prev_close", "previous_close", "open"))
    chg = _num(_first(inner, "change", "chg"))
    chg_pct = _num(_first(inner, "changePercent", "change_percent", "chg_percent", "change_pct"))
    if chg is None and prev:
        chg = price - prev
    if chg_pct is None and prev:
        chg_pct = (price - prev) / prev * 100
    return {
        "symbol": symbol, "price": price,
        "open": _num(_first(inner, "open")), "high": _num(_first(inner, "high")),
        "low": _num(_first(inner, "low")), "volume": _num(_first(inner, "volume")),
        "previousClose": prev, "change": chg, "changePercent": chg_pct,
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
                try:
                    norm["sparkline"] = [c["close"] for c in (_normalise_candles(await mcp_candles(client, symbol, "1D", 24)) or [])]
                except Exception:
                    pass
                return {"source": "mcp", **norm}
            return {"source": "mcp-raw", "symbol": symbol, "raw": raw,
                    "warning": "Could not map MCP quote fields — tell the assistant your MCP's tool/response format to add a mapping."}
        except MCPError as exc:
            return {"source": "demo", "mcpError": str(exc), **demo_data.quote(symbol)}
    return {"source": "demo", **demo_data.quote(symbol)}


@app.get("/api/quotes")
async def quotes(symbols: str = Query("", description="comma-separated")):
    syms = [s.strip().upper() for s in symbols.split(",") if s.strip()] or store.get("watchlist")
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


@app.get("/api/technicals")
async def technicals(symbol: str = Query(...), interval: str = "1D"):
    symbol = symbol.upper()
    if client.connected:
        try:
            raw = await mcp_technicals(client, symbol, interval)
            base = demo_data.technicals(symbol, interval)  # structure fallback
            if isinstance(raw, dict):
                rec = _first(raw, "recommendation", "RECOMMENDATION", "signal", "summary")
                indicators = _first(raw, "indicators", "values", "data") or {}
                if isinstance(rec, dict):
                    rec = _first(rec, "recommendation", "RECOMMENDATION", "signal") or str(rec)
                return {"source": "mcp", "symbol": symbol, "interval": interval,
                        "recommendation": str(rec) if rec else base["recommendation"],
                        "score": base["score"],
                        "indicators": indicators if isinstance(indicators, dict) else base["indicators"],
                        "raw": raw}
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
