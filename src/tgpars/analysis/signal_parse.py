"""Heuristic regex parser for signal posts.

Pulls a structured signal (asset, direction, entry, stop, targets, order type)
out of the human sub-traders' free-text posts so they can be price-verified in
bulk. Deliberately conservative: if a field is unclear it stays None, and the
caller can skip signals that are too incomplete to verify.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_NUM = r"\d+(?:[.,]\d+)?"


def _f(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return None


@dataclass
class ParsedSignal:
    asset: str | None = None
    direction: str | None = None  # long | short
    order_type: str | None = None  # market | limit
    entry_low: float | None = None
    entry_high: float | None = None
    stop: float | None = None
    targets: list[float] = field(default_factory=list)

    @property
    def is_verifiable(self) -> bool:
        return bool(self.asset and self.direction and self.entry_high and self.stop and self.targets)


def _direction(text: str) -> str | None:
    low = text.lower()
    if re.search(r"\b(long|лонг)\b|buy\b|📈", low):
        return "long"
    if re.search(r"\b(short|шорт)\b|sell\b|📉", low):
        return "short"
    return None


def _asset(text: str) -> str | None:
    m = re.search(r"COIN:\s*\$?([A-Za-z0-9]+/?[A-Za-z0-9]*)", text)
    if m:
        return m.group(1)
    m = re.search(r"\bXAU/?USD\b", text, re.IGNORECASE)
    if m:
        return "XAU/USD"
    m = re.search(r"[#$]([A-Za-z0-9]+/USDT)", text)
    if m:
        return m.group(1)
    m = re.search(r"[#$]([A-Za-z0-9]+USDT)\b", text)
    if m:
        return m.group(1)
    return None


def _entry(text: str) -> tuple[float | None, float | None, str | None]:
    order = None
    low_t = text.lower()
    if re.search(r"рынок|рыночн|market|по текущим", low_t):
        order = "market"
    elif re.search(r"лимит|limit", low_t):
        order = "limit"

    # ENTRY: a - b  /  Вход: a - b
    m = re.search(rf"(?:entry|вход)\s*:?\s*({_NUM})\s*[-–]\s*({_NUM})", text, re.IGNORECASE)
    if m:
        return _f(m.group(1)), _f(m.group(2)), order
    # 💲 3985 (Limit)
    m = re.search(rf"💲\s*({_NUM})", text)
    if m:
        return _f(m.group(1)), _f(m.group(1)), order or "limit"
    # по текущим (≈0.373)
    m = re.search(rf"≈\s*({_NUM})", text)
    if m:
        return _f(m.group(1)), _f(m.group(1)), order or "market"
    # Вход: 0.1316 рынок  |  Вход: рынок 74.7  |  Вход : 62707 рыночный
    m = re.search(rf"(?:вход|entry)\s*:?\s*(?:рынок|лимитка|лимит)?\s*({_NUM})", text, re.IGNORECASE)
    if m:
        return _f(m.group(1)), _f(m.group(1)), order
    return None, None, order


def _stop(text: str) -> float | None:
    m = re.search(rf"(?:stop\s*loss|стоп\s*лосс|стоп|sl)\s*:?\s*({_NUM})", text, re.IGNORECASE)
    return _f(m.group(1)) if m else None


def _targets(text: str) -> list[float]:
    # "TARGETS: a - b - c - ..."
    m = re.search(r"targets?\s*:?\s*([\d.,\s\-–]+)", text, re.IGNORECASE)
    if m and "-" in m.group(1):
        nums = re.findall(_NUM, m.group(1))
        if len(nums) >= 2:
            return [v for v in (_f(n) for n in nums) if v is not None]
    # Individual TP / ТП / Тейк / Target N lines.
    targets: list[float] = []
    for m in re.finditer(
        rf"(?:tp|тп|тейк(?:\s*профит)?|target)\s*\d*\s*:?\s*({_NUM})", text, re.IGNORECASE
    ):
        v = _f(m.group(1))
        if v is not None:
            targets.append(v)
    return targets


def parse_signal(text: str | None) -> ParsedSignal:
    if not text:
        return ParsedSignal()
    low, high, order = _entry(text)
    return ParsedSignal(
        asset=_asset(text),
        direction=_direction(text),
        order_type=order,
        entry_low=low,
        entry_high=high,
        stop=_stop(text),
        targets=_targets(text),
    )
