"""
Deterministic demo market data.

Used until a real TradingView MCP is connected (and as a fallback if the MCP
call for a symbol fails). Prices are a seeded random-walk ending near a
plausible base price, so the whole tracker (charts, P&L, technicals) is fully
usable offline. Clearly labelled "demo" in the API/UI — never presented as
real market data.
"""

from __future__ import annotations

import hashlib
import math
import random
from datetime import datetime, timedelta, timezone

BASE_PRICES = {
    "BTCUSDT": 97400.0, "BTCUSD": 97400.0, "ETHUSDT": 3420.0, "ETHUSD": 3420.0,
    "SOLUSDT": 214.0, "AAPL": 232.0, "NVDA": 138.0, "TSLA": 342.0, "MSFT": 428.0,
    "AMZN": 205.0, "GOOGL": 176.0, "META": 585.0, "EURUSD": 1.084, "GBPUSD": 1.271,
    "USDJPY": 154.2, "GOLD": 2665.0, "XAUUSD": 2665.0, "SPX": 5985.0, "SPX500": 5985.0,
    "USOIL": 71.5, "DOGEUSDT": 0.32, "XRPUSDT": 2.35,
}

KNOWN_SYMBOLS = [
    {"symbol": "BINANCE:BTCUSDT", "name": "Bitcoin / Tether", "type": "crypto", "exchange": "BINANCE"},
    {"symbol": "BINANCE:ETHUSDT", "name": "Ethereum / Tether", "type": "crypto", "exchange": "BINANCE"},
    {"symbol": "BINANCE:SOLUSDT", "name": "Solana / Tether", "type": "crypto", "exchange": "BINANCE"},
    {"symbol": "NASDAQ:AAPL", "name": "Apple Inc.", "type": "stock", "exchange": "NASDAQ"},
    {"symbol": "NASDAQ:NVDA", "name": "NVIDIA Corporation", "type": "stock", "exchange": "NASDAQ"},
    {"symbol": "NASDAQ:TSLA", "name": "Tesla Inc.", "type": "stock", "exchange": "NASDAQ"},
    {"symbol": "NASDAQ:MSFT", "name": "Microsoft Corporation", "type": "stock", "exchange": "NASDAQ"},
    {"symbol": "NASDAQ:AMZN", "name": "Amazon.com Inc.", "type": "stock", "exchange": "NASDAQ"},
    {"symbol": "FX:EURUSD", "name": "Euro / US Dollar", "type": "forex", "exchange": "FX"},
    {"symbol": "FX:GBPUSD", "name": "British Pound / US Dollar", "type": "forex", "exchange": "FX"},
    {"symbol": "TVC:GOLD", "name": "Gold (US$ / troy oz)", "type": "commodity", "exchange": "TVC"},
    {"symbol": "SP:SPX", "name": "S&P 500 Index", "type": "index", "exchange": "SP"},
]


def _seed(symbol: str, interval: str) -> int:
    return int(hashlib.sha256(f"{symbol}|{interval}".encode()).hexdigest()[:12], 16)


def base_price(symbol: str) -> float:
    bare = symbol.split(":")[-1].upper()
    if bare in BASE_PRICES:
        return BASE_PRICES[bare]
    # stable pseudo price between 5 and 500 for unknown symbols
    h = int(hashlib.sha256(bare.encode()).hexdigest()[:6], 16)
    return round(5 + (h % 49500) / 100.0, 2)


STEP = {"1": timedelta(minutes=1), "5": timedelta(minutes=5), "15": timedelta(minutes=15),
        "60": timedelta(hours=1), "240": timedelta(hours=4), "1D": timedelta(days=1),
        "D": timedelta(days=1), "1W": timedelta(weeks=1), "W": timedelta(weeks=1)}


def candles(symbol: str, interval: str = "1D", limit: int = 120) -> list[dict]:
    rng = random.Random(_seed(symbol, interval))
    step = STEP.get(interval, timedelta(days=1))
    end = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    start_price = base_price(symbol)
    # walk backwards from a plausible current price so "today" looks right
    closes = [start_price]
    vol = 0.018 if step >= timedelta(days=1) else 0.004
    for _ in range(limit - 1):
        drift = rng.gauss(0.0004, vol)
        closes.append(max(closes[-1] * (1 - drift), start_price * 0.05))
    closes.reverse()
    out = []
    t = end - step * (limit - 1)
    prev_close = closes[0]
    for i, close in enumerate(closes):
        open_ = prev_close
        high = max(open_, close) * (1 + abs(rng.gauss(0, vol / 2)))
        low = min(open_, close) * (1 - abs(rng.gauss(0, vol / 2)))
        volume = int(500_000 * (0.6 + rng.random() * 1.2) * (1 + abs(close - open_) / max(open_, 1e-9) * 20))
        out.append({
            "time": int((t + step * i).timestamp()),
            "open": round(open_, 6), "high": round(high, 6),
            "low": round(low, 6), "close": round(close, 6), "volume": volume,
        })
        prev_close = close
    return out


def quote(symbol: str) -> dict:
    cs = candles(symbol, "1D", 30)
    last, prev = cs[-1], cs[-2]
    change = last["close"] - prev["close"]
    return {
        "symbol": symbol,
        "price": last["close"],
        "open": last["open"], "high": last["high"], "low": last["low"],
        "previousClose": prev["close"], "volume": last["volume"],
        "change": round(change, 6),
        "changePercent": round(change / prev["close"] * 100, 4) if prev["close"] else 0,
        "sparkline": [c["close"] for c in cs[-24:]],
    }


def _sma(values: list[float], n: int) -> float | None:
    return round(sum(values[-n:]) / n, 6) if len(values) >= n else None


def technicals(symbol: str, interval: str = "1D") -> dict:
    cs = candles(symbol, interval, 120)
    closes = [c["close"] for c in cs]
    # RSI(14)
    gains = losses = 0.0
    for i in range(-14, 0):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    rsi = 100.0 if losses == 0 else round(100 - 100 / (1 + (gains / 14) / (losses / 14)), 2)
    sma20, sma50 = _sma(closes, 20), _sma(closes, 50)
    price = closes[-1]
    score = (1 if price > (sma20 or price) else -1) + (1 if price > (sma50 or price) else -1) \
        + (1 if rsi > 55 else -1 if rsi < 45 else 0)
    rec = "Strong Buy" if score >= 3 else "Buy" if score == 2 else "Neutral" if score in (0, 1, -1) else "Sell" if score == -2 else "Strong Sell"
    macd = round(((sma20 or price) - (sma50 or price)), 6)
    return {
        "symbol": symbol, "interval": interval,
        "recommendation": rec, "score": score,  # -3..+3
        "indicators": {
            "RSI(14)": rsi, "SMA 20": sma20, "SMA 50": sma50,
            "MACD (proxy)": macd, "Close": price,
            "Signal": "Buy" if macd > 0 else "Sell",
        },
    }


def search(query: str) -> list[dict]:
    q = query.strip().lower()
    if not q:
        return KNOWN_SYMBOLS
    hits = [s for s in KNOWN_SYMBOLS if q in s["symbol"].lower() or q in s["name"].lower()]
    if not hits and q.isalnum():
        sym = q.upper()
        hits = [{"symbol": sym if ":" in sym else f"NASDAQ:{sym}", "name": f"{sym} (custom symbol)",
                 "type": "unknown", "exchange": sym.split(':')[0] if ":" in sym else "NASDAQ"}]
    return hits
