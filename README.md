# 📈 Trading Tracker — TradingView MCP Edition

A personal trading tracker (watchlist, candlestick charts, technical rating,
open-position P&L, and a trade journal) whose market data comes from **your
TradingView MCP server** via the Model Context Protocol (MCP).

Until an MCP is connected it runs on clearly-labelled **demo data**, so every
feature works the moment you open it. Connect the MCP and the same screens
switch to live data — no code changes needed.

## Run it

```bash
pip install -r requirements.txt
uvicorn backend.app:app --host 0.0.0.0 --port 8000
# open http://localhost:8000
```

## Connect your TradingView MCP

Click **“🔌 Connect TradingView MCP”** in the app and paste one of:

| You have… | Choose transport | Paste |
|---|---|---|
| A server URL ending in `/mcp` | Streamable HTTP | `http://localhost:3000/mcp` (or your hosted URL) |
| A server URL ending in `/sse` | SSE | `http://localhost:3000/sse` |
| An npm/Python MCP package | stdio | command `npx`, args `-y your-package` (or `python`, `server.py`) |
| A GitHub repo link | — | Give the link to the assistant in chat; it will install & wire it |

Optional headers (e.g. `{"Authorization": "Bearer YOUR_KEY"}`) are supported.
Equivalent env vars are documented in `.env.example`
(`TRADINGVIEW_MCP_URL`, `TRADINGVIEW_MCP_COMMAND`, …).

### How the connection works

`backend/mcp_client.py` is a small transport-agnostic MCP client:

1. Handshake: `initialize` → `notifications/initialized`
2. Discovery: `tools/list` (tools show up in the **MCP Tool Explorer** tab)
3. Calls: `tools/call`, with an adapter that maps your server's tools onto
   quote / candles / technicals / search by matching tool names, descriptions
   and `inputSchema` fields (`symbol`, `exchange`, `interval`, `limit`, …).

Responses are normalised tolerantly (`price|close|last…`, `candles|ohlcv|bars…`,
JSON text blocks, `structuredContent`), so most TradingView MCP servers work
out of the box. If yours uses unusual tool names/shapes, open the Tool
Explorer, run a tool, and share the output — a mapping can then be added in
`backend/mcp_client.py` (`_find_tool` / `_build_args`) and
`backend/app.py` (the `_normalise_*` helpers).

## Features

- **Watchlist** — TradingView-format symbols (`NASDAQ:AAPL`, `BINANCE:BTCUSDT`,
  `FX:EURUSD`, `TVC:GOLD`…), live price, % change, sparklines, symbol search.
- **Charts** — built-in candlestick + volume chart (15m→1W), or switch to the
  embedded official **TradingView widget** for the selected symbol.
- **Technical rating** — gauge (Strong Sell → Strong Buy) + indicator table
  (from the MCP's technical-analysis tool when available).
- **Positions** — log long/short positions; P&L and market value update from
  current prices.
- **Trade journal** — record closed trades with strategy/notes; see total
  P&L, win rate, best/worst trade.
- **MCP Tool Explorer** — inspect and run every tool your MCP exposes.

Data you enter (watchlist, positions, trades, MCP config) is stored locally
in `data/*.json` (git-ignored).

## API (selected)

| Endpoint | Purpose |
|---|---|
| `GET /api/mcp/status` | connection state + discovered tools |
| `POST /api/mcp/config` | save + test an MCP connection |
| `POST /api/mcp/call` | call any MCP tool directly |
| `GET /api/quote?symbol=…` · `GET /api/quotes?symbols=a,b` | quotes |
| `GET /api/candles?symbol=…&interval=1D&limit=120` | OHLCV |
| `GET /api/technicals?symbol=…` | rating + indicators |
| `GET/POST/DELETE /api/watchlist`, `/api/positions`, `/api/trades` | tracker data |

Every market-data response carries `"source": "mcp" | "demo"` so you always
know whether you're looking at live or demo numbers.

> For tracking and education only — not financial advice.
