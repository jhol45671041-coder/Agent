# 📈 Trading Tracker — TradingView MCP Edition

A personal trading tracker (watchlist, candlestick charts, technical rating,
open-position P&L, and a trade journal) whose market data comes from a
**TradingView MCP server** via the Model Context Protocol (MCP) — including
the **official TradingView MCP** at `https://mcp.tradingview.com/mcp`, with
full **OAuth 2.1 sign-in** support.

Until an MCP is connected it runs on clearly-labelled **demo data**, so every
feature works the moment you open it. Connect the MCP and the same screens
switch to live data — no code changes needed.

## Run it

```bash
pip install -r requirements.txt
uvicorn backend.app:app --host 0.0.0.0 --port 8000
# open http://localhost:8000
```

> **Sign in from `localhost` (or an HTTPS address).** OAuth providers accept
> loopback (`localhost`/`127.0.0.1`) and HTTPS redirect addresses; a plain
> LAN-IP address may be rejected during sign-in.

## Connect the official TradingView MCP ⭐

Requires a **TradingView Essential plan or above** (trial/free plans don't
include MCP access). Authentication is OAuth 2.1 — you sign in with your
TradingView account; there is no API key, and your password never touches
this app.

1. Click **“🔌 Connect TradingView MCP”** (top-right). The official URL
   `https://mcp.tradingview.com/mcp` is pre-filled (or click the ⭐ preset).
2. Click **Test & Connect** — the tracker detects that sign-in is required.
3. Click **“Sign in with TradingView”**, sign in on tradingview.com, approve.
4. You're redirected back; the badge turns green **“LIVE · MCP”** and all
   quotes, candles, technicals and search are live TradingView data.
   Tokens refresh automatically; use **Sign out** in the same panel to revoke.

Under the hood (`backend/oauth.py`) the tracker performs the standard MCP
authorization flow: protected-resource discovery (RFC 9728) →
authorization-server metadata (RFC 8414) → dynamic client registration
(RFC 7591) → authorization code + PKCE S256 with the RFC 8707 `resource`
parameter → token storage/refresh/revocation. TradingView endpoints this
discovers automatically:

| Step | Endpoint |
|---|---|
| Resource metadata | `https://mcp.tradingview.com/.well-known/oauth-protected-resource/mcp` |
| Authorize | `https://www.tradingview.com/mcp/oauth/authorize` |
| Token | `https://www.tradingview.com/mcp/oauth/token` |
| Register | `https://www.tradingview.com/mcp/oauth/register` |
| Scopes | `mcp:read mcp:tools` |

Exact tool mapping for the official server (per TradingView's docs):

| Tracker feature | MCP tool | Notes |
|---|---|---|
| Quote (detail) | `get_symbol_data` | columns requested: close/open/high/low/change/change_abs/volume (`change` = %, `change_abs` = absolute) |
| Watchlist quotes | `get_symbol_data_batch` | one call for ≤ 50 symbols — stays inside the ~100 calls/min limit |
| Candlestick chart | `get_ohlcv` | intervals mapped `15→15m`, `60→1h`, `240→4h`, `1D`, `1W`; bars `{t,o,h,l,c,v}` |
| Technical rating | `get_technicals_rating` | RSI/MACD/… + Recommend value (−1…1) → gauge |
| Symbol search | `search_symbols` | fuzzy ticker/company search |

All other TradingView tools (screener, news, fundamentals, calendars,
watchlists, alerts…) are available in the app's **MCP Tool Explorer** tab.

> **Arena preview note:** the Arena sandbox that hosts the live preview can
> only reach a small allow-list of domains (GitHub/PyPI/npm) — it cannot
> open TLS connections to `tradingview.com`, so the sign-in round-trip from
> the preview server fails with a network error. Run the tracker on your
> own computer or any normal host (steps above) and the same sign-in
> completes in under a minute. The whole flow is tested end-to-end against
> `examples/mock_oauth_mcp.py`, a local replica of TradingView's discovery,
> OAuth and tool shapes.

## Connect a different MCP server

In the same **Connect** panel you can paste:

| You have… | Choose transport | Paste |
|---|---|---|
| A server URL ending in `/mcp` | Streamable HTTP | e.g. `http://localhost:3000/mcp` |
| A server URL ending in `/sse` | SSE | e.g. `http://localhost:3000/sse` |
| An npm/Python MCP package | stdio | command `npx`, args `-y your-package` (or `python`, `server.py`) |

Servers that answer 401 are handled with the same OAuth sign-in flow above;
optional static headers (e.g. `{"Authorization": "Bearer YOUR_KEY"}`) also
work. Equivalent env vars are in `.env.example`
(`TRADINGVIEW_MCP_URL`, `TRADINGVIEW_MCP_COMMAND`, …).

### How the generic connection works

`backend/mcp_client.py` is a transport-agnostic MCP client:
handshake (`initialize` → `notifications/initialized`, with
`MCP-Protocol-Version` / `Mcp-Session-Id` headers) → `tools/list` discovery →
`tools/call`, with an adapter that maps the server's tools onto
quote / candles / technicals / search by preferred tool names, descriptions
and `inputSchema` fields (schema `enum`s are honoured for intervals).
Responses are normalised tolerantly (JSON text blocks, `structuredContent`,
nested `data`, screener `change_abs`, Recommend values…), so most
TradingView-style MCP servers work out of the box.

## Features

- **Watchlist** — TradingView-format symbols (`NASDAQ:AAPL`, `BINANCE:BTCUSDT`,
  `FX:EURUSD`, `TVC:GOLD`…), price, % change, sparklines (demo mode), search.
- **Charts** — built-in candlestick + volume chart (15m→1W), or switch to the
  embedded official **TradingView widget** for the selected symbol.
- **Technical rating** — gauge (Strong Sell → Strong Buy) + indicator table.
- **Positions** — long/short positions with P&L from current prices.
- **Trade journal** — closed trades with strategy/notes; total P&L, win rate.
- **MCP Tool Explorer** — inspect and run every tool your MCP exposes.

Data you enter (watchlist, positions, trades, MCP config, OAuth tokens) is
stored locally in `data/*.json` (git-ignored). Tokens are kept separate from
the MCP config and masked in the UI.

## API (selected)

| Endpoint | Purpose |
|---|---|
| `GET /api/mcp/status` | connection state (`authRequired`, tools, error) |
| `POST /api/mcp/config` · `POST /api/mcp/call` | save/test a connection · call any tool |
| `GET /api/oauth/start` · `GET /oauth/callback` · `GET /api/oauth/status` · `DELETE /api/oauth` | OAuth 2.1 sign-in flow |
| `GET /api/quote?symbol=…` · `GET /api/quotes?symbols=a,b` | quotes (batch tool when available) |
| `GET /api/candles?symbol=…&interval=1D&limit=120` | OHLCV |
| `GET /api/technicals?symbol=…` · `GET /api/search?q=…` | rating + indicators · symbol search |
| `GET/POST/DELETE /api/watchlist`, `/api/positions`, `/api/trades` | tracker data |

Every market-data response carries `"source": "mcp" | "demo"` so you always
know whether you're looking at live or demo numbers.

## Test doubles

- `examples/fake_tradingview_mcp.py` — minimal stdio MCP server.
- `examples/mock_oauth_mcp.py` — local replica of the official TradingView
  MCP (OAuth discovery, dynamic registration, PKCE, real tool names/shapes)
  used to verify the full sign-in + data pipeline:
  `python3 examples/mock_oauth_mcp.py`, then connect the tracker to
  `http://127.0.0.1:8899/mcp`.

> For tracking and education only — not financial advice. TradingView MCP
> data may be delayed depending on your plan/exchange entitlements.
