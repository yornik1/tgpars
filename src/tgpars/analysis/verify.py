"""Deterministic price verification of parsed signals.

Given a structured signal (asset, direction, entry, stop, targets, posted_at),
this fetches real OHLCV from an exchange (via ccxt) starting at the signal time
and decides, objectively, whether each take-profit or the stop was reached
first. No LLM is involved — this is the ground truth used to cross-check what a
signaller *claims* happened.

Limitations:
  * Uses candle high/low touches; intrabar ordering of a same-candle TP+SL is
    resolved conservatively (stop assumed hit first).
  * Forex pairs (e.g. XAU/USD) are not on crypto exchanges; only crypto and
    tokenized-gold (XAUT/PAXG) assets verify. Others are marked unverifiable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import ccxt

# Map odd asset spellings to a tradable crypto base symbol.
_ASSET_ALIASES = {
    "XAU/USD": "XAUT",  # forex gold -> tokenized gold proxy (approximate)
    "XAUUSD": "XAUT",
    "XAUUSDT": "XAUT",
}


@dataclass
class VerifyResult:
    symbol: str | None
    verifiable: bool
    reason: str = ""
    entry_price: float | None = None
    sl_hit: bool = False
    targets_hit: int = 0
    first_target_time: str | None = None
    sl_time: str | None = None
    max_favorable_r: float | None = None
    max_adverse_r: float | None = None
    outcome: str | None = None  # tp_full | tp_partial | sl_hit | open | no_fill
    notes: list[str] = field(default_factory=list)


def normalize_symbol(asset: str) -> tuple[str, str] | None:
    """Return (base, quote) for a raw asset string, or None if unparseable."""
    a = asset.strip().upper().lstrip("$").replace("#", "")
    a = _ASSET_ALIASES.get(a, a)
    if "/" in a:
        base, _, quote = a.partition("/")
        return (base, quote or "USDT")
    m = re.match(r"^([A-Z0-9]+?)(USDT|USDC|USD)$", a)
    if m:
        quote = "USDT" if m.group(2) == "USD" else m.group(2)
        return (m.group(1), quote)
    if a.isalnum():
        return (a, "USDT")
    return None


def resolve_market(exchange: ccxt.Exchange, base: str, quote: str) -> str | None:
    """Find an available ccxt symbol for base/quote (spot preferred, then swap)."""
    spot = f"{base}/{quote}"
    if spot in exchange.markets:
        return spot
    swap = f"{base}/{quote}:{quote}"
    if swap in exchange.markets:
        return swap
    return None


def fetch_ohlcv_since(
    exchange: ccxt.Exchange,
    symbol: str,
    since_ms: int,
    *,
    timeframe: str = "5m",
    max_candles: int = 2000,
) -> list[list]:
    """Fetch OHLCV forward from since_ms, paginating up to max_candles."""
    out: list[list] = []
    cursor = since_ms
    while len(out) < max_candles:
        batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=1000)
        if not batch:
            break
        out.extend(batch)
        cursor = batch[-1][0] + 1
        if len(batch) < 1000:
            break
    return out[:max_candles]


def evaluate(
    *,
    direction: str,
    entry: float | None,
    stop: float | None,
    targets: list[float],
    order_type: str | None,
    candles: list[list],
) -> VerifyResult:
    """Walk candles and decide TP/SL outcome. direction is 'long' or 'short'."""
    res = VerifyResult(symbol=None, verifiable=True)
    if not candles:
        res.verifiable = False
        res.reason = "no candle data"
        return res

    is_long = direction == "long"
    # Entry fill: market -> first candle open; limit -> first candle that trades through entry.
    entry_price = entry
    if order_type == "market" or entry is None:
        entry_price = candles[0][1]  # open of first candle
    else:
        filled = False
        for ts, o, h, l, c, *_ in candles:
            if l <= entry <= h:
                filled = True
                break
        if not filled:
            res.outcome = "no_fill"
            res.reason = "limit entry never reached in window"
            res.entry_price = entry
            return res
    res.entry_price = entry_price

    if stop is None or entry_price is None:
        res.notes.append("no stop given; R metrics skipped")
    risk = abs(entry_price - stop) if stop is not None else None

    remaining = sorted(targets) if is_long else sorted(targets, reverse=True)
    best = entry_price
    worst = entry_price
    for ts, o, h, l, c, *_ in candles:
        best = max(best, h) if is_long else min(best, l)
        worst = min(worst, l) if is_long else max(worst, h)
        # targets reached this candle
        while remaining:
            tgt = remaining[0]
            hit = h >= tgt if is_long else l <= tgt
            if hit:
                res.targets_hit += 1
                if res.first_target_time is None:
                    res.first_target_time = _iso(ts)
                remaining.pop(0)
            else:
                break
        # stop reached
        if stop is not None and (l <= stop if is_long else h >= stop):
            res.sl_hit = True
            res.sl_time = _iso(ts)
            break

    if risk:
        res.max_favorable_r = round((best - entry_price) / risk * (1 if is_long else -1), 2)
        res.max_adverse_r = round((worst - entry_price) / risk * (1 if is_long else -1), 2)

    total_targets = len(targets)
    if res.targets_hit and res.targets_hit >= total_targets and total_targets:
        res.outcome = "tp_full"
    elif res.targets_hit:
        res.outcome = "tp_partial"
    elif res.sl_hit:
        res.outcome = "sl_hit"
    else:
        res.outcome = "open"
    return res


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()


def verify_signal(
    *,
    asset: str,
    direction: str,
    posted_at: datetime,
    entry: float | None,
    stop: float | None,
    targets: list[float],
    order_type: str | None = None,
    exchange_id: str = "binance",
    timeframe: str = "5m",
    exchange: ccxt.Exchange | None = None,
) -> VerifyResult:
    """High-level: resolve symbol, fetch prices, evaluate outcome.

    Pass a pre-loaded ``exchange`` to verify many signals without reloading
    markets each time.
    """
    pair = normalize_symbol(asset)
    if pair is None:
        return VerifyResult(symbol=None, verifiable=False, reason=f"cannot parse asset {asset!r}")
    base, quote = pair

    if exchange is None:
        exchange = getattr(ccxt, exchange_id)({"enableRateLimit": True})
        exchange.load_markets()
    symbol = resolve_market(exchange, base, quote)
    if symbol is None:
        return VerifyResult(
            symbol=f"{base}/{quote}", verifiable=False, reason="symbol not on exchange"
        )

    since_ms = int(posted_at.timestamp() * 1000)
    candles = fetch_ohlcv_since(exchange, symbol, since_ms, timeframe=timeframe)
    result = evaluate(
        direction=direction,
        entry=entry,
        stop=stop,
        targets=targets,
        order_type=order_type,
        candles=candles,
    )
    result.symbol = symbol
    if asset.strip().upper().lstrip("$#") in _ASSET_ALIASES:
        result.notes.append("forex gold approximated by XAUT/USDT")
    return result
