"""Tiny JSON-file store for watchlist / positions / trades / MCP config.

Data lives in <repo>/data/store.json (git-ignored) so the tracker works with
zero setup. Swap this module for SQLite/Postgres later without touching the API.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STORE_FILE = DATA_DIR / "store.json"
CONFIG_FILE = DATA_DIR / "mcp_config.json"

_lock = threading.Lock()

DEFAULTS = {
    "watchlist": ["BINANCE:BTCUSDT", "BINANCE:ETHUSDT", "NASDAQ:AAPL", "NASDAQ:NVDA", "FX:EURUSD", "TVC:GOLD"],
    "positions": [],   # {id, symbol, side: long|short, quantity, entryPrice, openedAt, note}
    "trades": [],      # {id, symbol, side, quantity, entryPrice, exitPrice, openedAt, closedAt, strategy, note}
}


def _read() -> dict:
    if STORE_FILE.exists():
        try:
            data = json.loads(STORE_FILE.read_text())
            return {**DEFAULTS, **data}
        except (json.JSONDecodeError, OSError):
            pass
    return dict(DEFAULTS)


def _write(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STORE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(STORE_FILE)


def get(key: str):
    with _lock:
        return _read().get(key, [])


def add_watch(symbol: str) -> list:
    symbol = symbol.strip().upper()
    with _lock:
        data = _read()
        if symbol and symbol not in data["watchlist"]:
            data["watchlist"].append(symbol)
            _write(data)
        return data["watchlist"]


def remove_watch(symbol: str) -> list:
    symbol = symbol.strip().upper()
    with _lock:
        data = _read()
        data["watchlist"] = [s for s in data["watchlist"] if s != symbol]
        _write(data)
        return data["watchlist"]


def add_item(key: str, item: dict) -> dict:
    item = {"id": uuid.uuid4().hex[:10], **item}
    with _lock:
        data = _read()
        data[key].insert(0, item)
        _write(data)
    return item


def delete_item(key: str, item_id: str) -> bool:
    with _lock:
        data = _read()
        before = len(data[key])
        data[key] = [i for i in data[key] if i.get("id") != item_id]
        _write(data)
        return len(data[key]) != before


# -- MCP config (kept separate, also git-ignored) ---------------------------- #
def load_mcp_config() -> dict:
    """Env vars win over the saved file, so deployments stay 12-factor."""
    env_cfg = {}
    if os.getenv("TRADINGVIEW_MCP_URL") or os.getenv("TRADINGVIEW_MCP_COMMAND"):
        env_cfg = {
            "transport": os.getenv("TRADINGVIEW_MCP_TRANSPORT", "streamable-http" if os.getenv("TRADINGVIEW_MCP_URL") else "stdio"),
            "url": os.getenv("TRADINGVIEW_MCP_URL", ""),
            "command": os.getenv("TRADINGVIEW_MCP_COMMAND", ""),
            "args": os.getenv("TRADINGVIEW_MCP_ARGS", ""),
        }
        if os.getenv("TRADINGVIEW_MCP_API_KEY"):
            env_cfg["headers"] = {"Authorization": f"Bearer {os.environ['TRADINGVIEW_MCP_API_KEY']}"}
        return env_cfg
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_mcp_config(cfg: dict | None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if cfg is None:
        CONFIG_FILE.unlink(missing_ok=True)
    else:
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
