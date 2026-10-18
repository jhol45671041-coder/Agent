#!/usr/bin/env python3
"""
Minimal example of a TradingView-style MCP server (stdio transport).

This is NOT a real data source — it returns fixed sample values. It exists so
you can (a) verify the tracker's MCP connection end-to-end, and (b) see the
exact tool shapes the tracker understands, as a reference for wiring a real
TradingView MCP.

Connect it from the UI:  transport = stdio, command = python3,
args = examples/fake_tradingview_mcp.py
"""
import json
import sys
import time

TOOLS = [
    {"name": "get_quote", "description": "Get the latest quote/price for a symbol",
     "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "get_historical_data", "description": "Get historical OHLCV candles for a symbol",
     "inputSchema": {"type": "object", "properties": {
         "symbol": {"type": "string"}, "interval": {"type": "string"}, "limit": {"type": "integer"}},
         "required": ["symbol"]}},
    {"name": "get_technical_analysis", "description": "Technical analysis summary and indicators",
     "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}, "interval": {"type": "string"}}}},
    {"name": "search_symbol", "description": "Search symbols by name or ticker",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}},
]


def tool_result(payload) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def call(name: str, args: dict) -> dict:
    symbol = str(args.get("symbol") or args.get("query") or "NASDAQ:AAPL").upper()
    if name == "get_quote":
        return tool_result({"symbol": symbol, "price": 250.25, "open": 247.0, "high": 252.1,
                            "low": 246.3, "previousClose": 246.8, "volume": 1234567,
                            "change": 3.45, "changePercent": 1.4})
    if name == "get_historical_data":
        limit = int(args.get("limit") or 30)
        now = int(time.time())
        candles = [{"time": now - (limit - i) * 86400, "open": 240 + i * 0.3, "high": 242 + i * 0.3,
                    "low": 239 + i * 0.3, "close": 241 + i * 0.3, "volume": 900000 + i * 1000}
                   for i in range(limit)]
        return tool_result({"symbol": symbol, "candles": candles})
    if name == "get_technical_analysis":
        return tool_result({"symbol": symbol, "recommendation": "BUY",
                            "indicators": {"RSI": 61.2, "MACD": 1.85, "SMA20": 244.0, "SMA50": 238.5}})
    if name == "search_symbol":
        q = str(args.get("query") or symbol)
        return tool_result([{"symbol": f"NASDAQ:{q.upper()}", "description": f"{q.upper()} (example result)",
                             "type": "stock", "exchange": "NASDAQ"}])
    return {"content": [{"type": "text", "text": f"unknown tool {name}"}], "isError": True}


def main() -> None:
    while True:
        line = sys.stdin.readline()
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, mid = msg.get("method"), msg.get("id")
        if mid is None:
            continue  # notification
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fake-tradingview-mcp", "version": "0.1.0"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            p = msg.get("params") or {}
            result = call(p.get("name", ""), p.get("arguments") or {})
        elif method == "ping":
            result = {}
        else:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid,
                                         "error": {"code": -32601, "message": f"method not found: {method}"}}) + "\n")
            sys.stdout.flush()
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
