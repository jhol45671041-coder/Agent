#!/usr/bin/env python3
"""
Local test double for the official TradingView MCP (https://mcp.tradingview.com/mcp).

Replicates, on http://127.0.0.1:8899 :
  * 401 + WWW-Authenticate with resource_metadata (RFC 9728)
  * /.well-known/oauth-protected-resource/mcp
  * /.well-known/oauth-authorization-server  (authorize/token/register/revoke)
  * Dynamic client registration + authorization-code flow with PKCE S256
  * The real tool names & parameters: get_ohlcv, get_symbol_data,
    get_symbol_data_batch, get_technicals_rating, search_symbols
    returning TradingView-shaped payloads (bars as {t,o,h,l,c,v},
    screener change = %, change_abs = absolute).

Used to test the tracker's OAuth + adapter pipeline end-to-end without a
TradingView account. NOT a real data source.

Run:  python3 examples/mock_oauth_mcp.py   (needs fastapi + uvicorn)
"""
import base64
import hashlib
import time
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

BASE = "http://127.0.0.1:8899"
ACCESS_TOKEN = "mock-access-token"

app = FastAPI(title="mock-tradingview-mcp")
clients: dict[str, dict] = {}
codes: dict[str, dict] = {}
test_state: dict = {"last_interval": None, "last_tool": None, "calls": []}

INTERVALS = ["1m", "5m", "15m", "30m", "1h", "2h", "4h", "1D", "1W", "1M"]
TOOLS = [
    {"name": "get_ohlcv", "description": "Fetch historical OHLCV bars for a symbol at an interval",
     "inputSchema": {"type": "object", "properties": {
         "symbol": {"type": "string"}, "interval": {"type": "string", "enum": INTERVALS, "default": "1D"},
         "count": {"type": "integer", "default": 300}, "summary": {"type": "boolean"}},
         "required": ["symbol"]}},
    {"name": "get_symbol_data", "description": "Fetch TradingView screener columns for one symbol",
     "inputSchema": {"type": "object", "properties": {
         "symbol": {"type": "string"}, "columns": {"type": "array", "items": {"type": "string"}}},
         "required": ["symbol"]}},
    {"name": "get_symbol_data_batch", "description": "Quotes/columns for up to 50 symbols in one call",
     "inputSchema": {"type": "object", "properties": {
         "symbols": {"type": "array", "items": {"type": "string"}},
         "columns": {"type": "array", "items": {"type": "string"}}}, "required": ["symbols"]}},
    {"name": "get_technicals_rating", "description": "Full single-timeframe indicator snapshot",
     "inputSchema": {"type": "object", "properties": {
         "symbol": {"type": "string"}, "interval": {"type": "string", "enum": INTERVALS, "default": "1D"}},
         "required": ["symbol"]}},
    {"name": "search_symbols", "description": "Resolve a ticker or company name into symbols",
     "inputSchema": {"type": "object", "properties": {
         "query": {"type": "string"}, "type_filter": {"type": "string"}}, "required": ["query"]}},
]


# --- OAuth discovery ------------------------------------------------------- #
@app.get("/.well-known/oauth-protected-resource/mcp")
async def protected_resource():
    return {"resource": f"{BASE}/mcp", "authorization_servers": [BASE],
            "bearer_methods_supported": ["header"], "scopes_supported": ["mcp:read", "mcp:tools"]}


@app.get("/.well-known/oauth-authorization-server")
async def as_metadata():
    return {"issuer": BASE, "authorization_endpoint": f"{BASE}/authorize",
            "token_endpoint": f"{BASE}/token", "registration_endpoint": f"{BASE}/register",
            "revocation_endpoint": f"{BASE}/revoke", "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"], "scopes_supported": ["mcp:read", "mcp:tools"]}


@app.post("/register")
async def register(req: Request):
    body = await req.json()
    cid = "mock-client-1"
    clients[cid] = body
    return JSONResponse({"client_id": cid, "token_endpoint_auth_method": "none",
                         "redirect_uris": body.get("redirect_uris", [])}, status_code=201)


@app.get("/authorize")
async def authorize(client_id: str = "", redirect_uri: str = "", state: str = "",
                    code_challenge: str = "", code_challenge_method: str = "S256",
                    scope: str = "", resource: str = "", response_type: str = "code"):
    if client_id not in clients or response_type != "code":
        return JSONResponse({"error": "unauthorized_client"}, status_code=400)
    code = "mock-auth-code"
    codes[code] = {"challenge": code_challenge, "client_id": client_id}
    return RedirectResponse(f"{redirect_uri}?{urlencode({'code': code, 'state': state})}", status_code=302)


def _token_payload():
    return {"access_token": ACCESS_TOKEN, "token_type": "Bearer", "expires_in": 3600,
            "refresh_token": "mock-refresh-token", "scope": "mcp:read mcp:tools"}


async def _form(req: Request) -> dict:
    from urllib.parse import parse_qs
    body = (await req.body()).decode()
    return {k: v[0] for k, v in parse_qs(body).items()}


@app.post("/token")
async def token(req: Request):
    form = await _form(req)
    if form.get("grant_type") == "refresh_token":
        return _token_payload() if form.get("refresh_token") == "mock-refresh-token" else JSONResponse({"error": "invalid_grant"}, status_code=400)
    saved = codes.pop(form.get("code", ""), None)
    if not saved:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    digest = base64.urlsafe_b64encode(hashlib.sha256(form.get("code_verifier", "").encode()).digest()).decode().rstrip("=")
    if digest != saved["challenge"]:
        return JSONResponse({"error": "invalid_grant", "error_description": "PKCE verification failed"}, status_code=400)
    return _token_payload()


@app.post("/revoke")
async def revoke(req: Request):
    return {}


# --- MCP endpoint ------------------------------------------------------------ #
def _quote(symbol: str) -> dict:
    bare = symbol.split(":")[-1]
    return {"symbol": symbol, "data": {"name": bare, "description": f"{bare} (mock)",
            "close": 251.5, "open": 248.0, "high": 253.0, "low": 247.0,
            "change": 1.41, "change_abs": 3.5, "volume": 1234567}}


def _tool_call(name: str, args: dict) -> dict:
    import json as _json
    test_state["last_tool"] = name
    test_state["calls"].append(name)
    symbol = str(args.get("symbol") or "NASDAQ:AAPL").upper()
    if name == "get_symbol_data":
        payload = _quote(symbol)
    elif name == "get_symbol_data_batch":
        payload = {"results": [_quote(s.upper()) for s in args.get("symbols") or [symbol]], "missing": []}
    elif name == "get_ohlcv":
        interval = args.get("interval", "1D")
        test_state["last_interval"] = interval
        if interval not in INTERVALS:
            return {"content": [{"type": "text", "text": f"invalid interval {interval}"}], "isError": True}
        count = int(args.get("count") or 30)
        now = int(time.time())
        payload = {"symbol": symbol, "interval": interval, "bars": [
            {"t": now - (count - i) * 86400, "o": 240 + i * 0.3, "h": 242 + i * 0.3,
             "l": 239 + i * 0.3, "c": 241 + i * 0.3, "v": 900000 + i * 1000} for i in range(count)]}
    elif name == "get_technicals_rating":
        payload = {"symbol": symbol, "interval": args.get("interval", "1D"),
                   "indicators": {"RSI": 62.3, "MACD": 1.9, "EMA20": 246.1, "SMA50": 238.4, "ADX": 28.0},
                   "recommend": {"summary": 0.62, "moving_averages": 0.58, "oscillators": 0.4}}
    elif name == "search_symbols":
        q = str(args.get("query") or symbol).upper()
        payload = [{"symbol": f"NASDAQ:{q}", "description": f"{q} Inc (mock)",
                    "type": "stock", "exchange": "NASDAQ", "logoid": ""}]
    else:
        return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}
    return {"content": [{"type": "text", "text": _json.dumps(payload)}]}


@app.post("/mcp")
async def mcp(req: Request):
    if req.headers.get("authorization") != f"Bearer {ACCESS_TOKEN}":
        return JSONResponse(
            {"detail": "This server requires OAuth authentication"}, status_code=401,
            headers={"WWW-Authenticate":
                     f'Bearer resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp", '
                     'scope="mcp:read mcp:tools"'})
    body = await req.json()
    mid, method = body.get("id"), body.get("method")
    if mid is None:
        return JSONResponse(status_code=202, content={})
    if method == "initialize":
        result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "mock-tradingview-mcp", "version": "1.0.0"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        p = body.get("params") or {}
        result = _tool_call(p.get("name", ""), p.get("arguments") or {})
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


@app.get("/__test__/last")
async def last():
    return test_state


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8899)
