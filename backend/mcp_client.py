"""
Generic MCP (Model Context Protocol) client for a TradingView MCP server.

Supports the three ways TradingView MCP servers are usually distributed:
  1. streamable-http  ->  POST JSON-RPC to a URL  (e.g. http://localhost:3000/mcp)
  2. sse              ->  legacy SSE transport    (e.g. http://localhost:3000/sse)
  3. stdio            ->  spawn a local command   (e.g. npx -y <package>  /  python server.py)

The client performs the MCP handshake (initialize -> notifications/initialized),
discovers tools with tools/list, and calls them with tools/call.

A small "adapter" layer then maps whatever tools the server exposes onto the
four things the tracker needs (quote / candles / technicals / search) by
inspecting tool names + inputSchemas, so it works with most TradingView MCP
servers without code changes. If a mapping fails, callers get the raw result
and the UI can fall back to demo data.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import time
from typing import Any, Optional

import httpx

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "trading-tracker", "version": "1.0.0"}


class MCPError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Base JSON-RPC client (transport agnostic)
# --------------------------------------------------------------------------- #
class MCPClient:
    def __init__(self, config: dict):
        self.config = config or {}
        self.transport = (self.config.get("transport") or "").strip().lower()
        self.url = (self.config.get("url") or "").strip()
        self.command = (self.config.get("command") or "").strip()
        self.args = self.config.get("args") or []
        if isinstance(self.args, str):
            self.args = shlex.split(self.args)
        self.headers = self.config.get("headers") or {}
        self.extra_env = self.config.get("env") or {}

        self._id = 0
        self._session_id: Optional[str] = None
        self.server_info: dict = {}
        self.tools: list[dict] = []
        self.connected = False
        self.last_error: Optional[str] = None
        self.last_connected_at: Optional[float] = None

        # stdio state
        self._proc: Optional[asyncio.subprocess.Process] = None
        # sse state
        self._sse_task: Optional[asyncio.Task] = None
        self._sse_endpoint: Optional[str] = None
        self._pending: dict[int, asyncio.Future] = {}

    # -- helpers ---------------------------------------------------------- #
    def _next_id(self) -> int:
        self._id += 1
        return self._id

    @property
    def configured(self) -> bool:
        if self.transport in ("streamable-http", "http", "sse"):
            return bool(self.url)
        if self.transport == "stdio":
            return bool(self.command)
        return False

    def public_config(self) -> dict:
        """Config safe to show in the UI (secrets in headers masked)."""
        headers = {}
        for k, v in (self.headers or {}).items():
            headers[k] = "••••••" if any(s in k.lower() for s in ("key", "token", "auth", "secret")) else v
        return {
            "transport": self.transport,
            "url": self.url,
            "command": self.command,
            "args": self.args,
            "headers": headers,
            "hasEnv": bool(self.extra_env),
        }

    def status(self) -> dict:
        return {
            "configured": self.configured,
            "connected": self.connected,
            "mode": "live" if self.connected else "demo",
            "serverInfo": self.server_info,
            "tools": [
                {"name": t.get("name"), "description": t.get("description", ""), "inputSchema": t.get("inputSchema", {})}
                for t in self.tools
            ],
            "error": self.last_error,
            "lastConnectedAt": self.last_connected_at,
            "config": self.public_config(),
        }

    # -- lifecycle -------------------------------------------------------- #
    async def connect(self, timeout: float = 20.0) -> dict:
        self.connected = False
        self.last_error = None
        self.tools = []
        self.server_info = {}
        if not self.configured:
            self.last_error = "No MCP configured yet. Paste your TradingView MCP URL/command in the Connect panel."
            return self.status()
        try:
            if self.transport == "stdio":
                await self._stdio_start()
            elif self.transport == "sse":
                await self._sse_start(timeout)
            # streamable-http needs no persistent connection; handshake per client

            init = await self._request(
                "initialize",
                {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": CLIENT_INFO},
                timeout=timeout,
            )
            if isinstance(init, dict):
                self.server_info = init.get("serverInfo") or {}
            await self._notify("notifications/initialized", {}, timeout=timeout)
            listed = await self._request("tools/list", {}, timeout=timeout)
            self.tools = (listed or {}).get("tools", []) if isinstance(listed, dict) else []
            self.connected = True
            self.last_connected_at = time.time()
        except Exception as exc:  # noqa: BLE001 - surface any transport error to UI
            self.last_error = str(exc)
            self.connected = False
            await self.disconnect()
        return self.status()

    async def disconnect(self) -> None:
        if self._sse_task:
            self._sse_task.cancel()
            self._sse_task = None
        if self._proc and self._proc.returncode is None:
            try:
                self._proc.terminate()
                await asyncio.wait_for(self._proc.wait(), timeout=3)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
        self._proc = None
        self.connected = False

    async def call_tool(self, name: str, arguments: dict | None = None, timeout: float = 30.0) -> Any:
        if not self.connected:
            st = await self.connect(timeout=min(timeout, 20))
            if not st["connected"]:
                raise MCPError(self.last_error or "MCP not connected")
        result = await self._request("tools/call", {"name": name, "arguments": arguments or {}}, timeout=timeout)
        return _unwrap_tool_result(result)

    # -- JSON-RPC dispatch ------------------------------------------------- #
    async def _request(self, method: str, params: dict, timeout: float = 20.0) -> Any:
        rid = self._next_id()
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        if self.transport in ("streamable-http", "http"):
            return await self._http_request(payload, rid, timeout)
        if self.transport == "sse":
            return await self._sse_request(payload, rid, timeout)
        if self.transport == "stdio":
            return await self._stdio_request(payload, rid, timeout)
        raise MCPError(f"Unknown transport: {self.transport!r} (use streamable-http, sse, or stdio)")

    async def _notify(self, method: str, params: dict, timeout: float = 20.0) -> None:
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        try:
            if self.transport in ("streamable-http", "http"):
                await self._http_post(payload, timeout, expect_body=False)
            elif self.transport == "sse":
                await self._sse_post(payload, timeout)
            elif self.transport == "stdio":
                await self._stdio_send(payload)
        except Exception:
            pass  # some servers don't require / reject the notification; ignore

    # -- streamable HTTP ---------------------------------------------------- #
    async def _http_post(self, payload: dict, timeout: float, expect_body: bool = True) -> httpx.Response:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self.headers,
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.post(self.url, json=payload, headers=headers)
        sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid
        if expect_body and resp.status_code >= 400:
            raise MCPError(f"MCP HTTP {resp.status_code}: {resp.text[:400]}")
        return resp

    async def _http_request(self, payload: dict, rid: int, timeout: float) -> Any:
        resp = await self._http_post(payload, timeout)
        ctype = resp.headers.get("content-type", "")
        messages: list[dict] = []
        if "text/event-stream" in ctype:
            for line in resp.text.splitlines():
                line = line.strip()
                if line.startswith("data:"):
                    try:
                        messages.append(json.loads(line[5:].strip()))
                    except json.JSONDecodeError:
                        pass
        else:
            try:
                data = resp.json()
                messages = data if isinstance(data, list) else [data]
            except Exception:
                raise MCPError(f"MCP returned non-JSON response: {resp.text[:300]}")
        for msg in messages:
            if isinstance(msg, dict) and msg.get("id") == rid:
                if "error" in msg:
                    raise MCPError(f"MCP error: {json.dumps(msg['error'])[:400]}")
                return msg.get("result")
        raise MCPError("MCP response contained no matching JSON-RPC result")

    # -- legacy SSE ---------------------------------------------------------- #
    async def _sse_start(self, timeout: float) -> None:
        self._sse_endpoint = None
        ready = asyncio.Event()

        async def reader():
            headers = {"Accept": "text/event-stream", **self.headers}
            async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, read=None), follow_redirects=True) as client:
                async with client.stream("GET", self.url, headers=headers) as resp:
                    if resp.status_code >= 400:
                        raise MCPError(f"SSE HTTP {resp.status_code}")
                    event = ""
                    async for line in resp.aiter_lines():
                        line = line.strip()
                        if line.startswith("event:"):
                            event = line[6:].strip()
                        elif line.startswith("data:"):
                            data = line[5:].strip()
                            if event == "endpoint":
                                # data is a path like /messages/?session_id=...
                                base = httpx.URL(self.url)
                                self._sse_endpoint = str(base.copy_with(path=data.split("?")[0], query=data.split("?", 1)[1] if "?" in data else "")) if not data.startswith("http") else data
                                ready.set()
                            else:
                                try:
                                    msg = json.loads(data)
                                except json.JSONDecodeError:
                                    continue
                                mid = msg.get("id")
                                fut = self._pending.pop(mid, None) if mid is not None else None
                                if fut and not fut.done():
                                    if "error" in msg:
                                        fut.set_exception(MCPError(f"MCP error: {json.dumps(msg['error'])[:400]}"))
                                    else:
                                        fut.set_result(msg.get("result"))

        self._sse_task = asyncio.create_task(reader())
        try:
            await asyncio.wait_for(ready.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise MCPError("Timed out waiting for SSE endpoint event from MCP server")

    async def _sse_post(self, payload: dict, timeout: float) -> None:
        if not self._sse_endpoint:
            raise MCPError("SSE endpoint not established")
        headers = {"Content-Type": "application/json", **self.headers}
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(self._sse_endpoint, json=payload, headers=headers)
        if resp.status_code >= 400:
            raise MCPError(f"SSE POST HTTP {resp.status_code}: {resp.text[:300]}")

    async def _sse_request(self, payload: dict, rid: int, timeout: float) -> Any:
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        await self._sse_post(payload, timeout)
        return await asyncio.wait_for(fut, timeout=timeout)

    # -- stdio --------------------------------------------------------------- #
    async def _stdio_start(self) -> None:
        if self._proc and self._proc.returncode is None:
            return
        env = {**os.environ, **{k: str(v) for k, v in self.extra_env.items()}}
        self._proc = await asyncio.create_subprocess_exec(
            self.command, *self.args,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=env,
        )

    async def _stdio_send(self, payload: dict) -> None:
        if not self._proc or not self._proc.stdin:
            raise MCPError("stdio MCP process not running")
        self._proc.stdin.write((json.dumps(payload) + "\n").encode())
        await self._proc.stdin.drain()

    async def _stdio_request(self, payload: dict, rid: int, timeout: float) -> Any:
        await self._stdio_send(payload)
        assert self._proc and self._proc.stdout

        async def read_until_match():
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    raise MCPError(
                        "stdio MCP process closed stdout (it exited or crashed). "
                        "Check data/mcp_stdio.log for its error output, and verify the command/args."
                    )
                try:
                    msg = json.loads(line.decode().strip())
                except json.JSONDecodeError:
                    continue  # ignore stray non-JSON log lines on stdout
                if msg.get("id") == rid:
                    if "error" in msg:
                        raise MCPError(f"MCP error: {json.dumps(msg['error'])[:400]}")
                    return msg.get("result")

        return await asyncio.wait_for(read_until_match(), timeout=timeout)


# --------------------------------------------------------------------------- #
# Result unwrapping + TradingView adapter
# --------------------------------------------------------------------------- #
def _unwrap_tool_result(result: Any) -> Any:
    """MCP tools/call result -> python object (parse JSON text blocks)."""
    if isinstance(result, dict):
        if result.get("structuredContent") is not None:
            return result["structuredContent"]
        content = result.get("content")
        if isinstance(content, list):
            texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
            if texts:
                joined = "\n".join(texts)
                try:
                    return json.loads(joined)
                except json.JSONDecodeError:
                    return {"raw": joined, "isError": bool(result.get("isError"))}
    return result


def _find_tool(tools: list[dict], *keywords: str) -> Optional[dict]:
    best, best_score = None, 0
    for tool in tools:
        hay = f"{tool.get('name','')} {tool.get('description','')}".lower()
        score = sum(1 for kw in keywords if kw.lower() in hay)
        if score > best_score:
            best, best_score = tool, score
    return best if best_score else None


def _build_args(tool: dict, symbol: str, interval: str = "1D", limit: int = 120, query: str = "") -> dict:
    """Fill a tool's inputSchema with sensible values for a TradingView symbol."""
    schema = (tool.get("inputSchema") or {}).get("properties") or {}
    exchange, _, bare = symbol.partition(":")
    if not bare:
        exchange, bare = "", symbol
    args: dict[str, Any] = {}
    for key in schema:
        lk = key.lower()
        if lk in ("symbol", "ticker", "pair", "instrument", "asset"):
            # If schema also has exchange, pass bare ticker; else full EXCHANGE:SYMBOL
            has_exchange = any(k.lower() == "exchange" for k in schema)
            args[key] = bare if (has_exchange and exchange) else symbol
        elif lk == "exchange":
            args[key] = exchange or "NASDAQ"
        elif lk in ("interval", "timeframe", "resolution", "period", "time_frame"):
            args[key] = interval
        elif lk in ("limit", "count", "bars", "n", "nbars", "length"):
            args[key] = limit
        elif lk in ("query", "search", "text", "q", "keyword"):
            args[key] = query or bare
    if not args:  # schema-less tools: try the most common convention
        args = {"symbol": symbol}
        if query:
            args = {"query": query}
    return args


async def mcp_quote(client: MCPClient, symbol: str) -> Any:
    tool = _find_tool(client.tools, "quote", "price", "ticker", "get_symbol", "snapshot")
    if not tool:
        raise MCPError("No quote/price tool found on MCP server")
    return await client.call_tool(tool["name"], _build_args(tool, symbol))


async def mcp_candles(client: MCPClient, symbol: str, interval: str, limit: int) -> Any:
    tool = _find_tool(client.tools, "candle", "ohlcv", "history", "historical", "bars", "chart_data", "kline")
    if not tool:
        raise MCPError("No candles/history tool found on MCP server")
    return await client.call_tool(tool["name"], _build_args(tool, symbol, interval, limit))


async def mcp_technicals(client: MCPClient, symbol: str, interval: str) -> Any:
    tool = _find_tool(client.tools, "technical", "analysis", "indicator", "recommendation", "rsi", "summary")
    if not tool:
        raise MCPError("No technical-analysis tool found on MCP server")
    return await client.call_tool(tool["name"], _build_args(tool, symbol, interval))


async def mcp_search(client: MCPClient, query: str) -> Any:
    tool = _find_tool(client.tools, "search", "lookup", "find_symbol", "screener")
    if not tool:
        raise MCPError("No search tool found on MCP server")
    return await client.call_tool(tool["name"], _build_args(tool, query, query=query))
