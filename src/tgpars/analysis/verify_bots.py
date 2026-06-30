"""Verify strategy-bot trades (Channel A «Стратегия ...» posts).

The bots are explicit: the OPEN states entry/stop/target/direction, the CLOSE
states entry→exit and a result in R. That lets us check two things:

  1. Arithmetic consistency (no network): does the claimed R equal the R implied
     by the stated entry/exit/stop?  R = (exit-entry)/(entry-stop), sign-flipped
     for shorts. A 'цель достигнута' close should exit at ~target; a 'поймала
     стоп' close at ~stop.
  2. (optional) Real prices: whether the exit level actually traded after entry.

Pairs OPEN and CLOSE by (strategy, asset, entry price).
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select

from ..config import load_settings
from ..db.models import Message
from ..db.session import create_db_engine, make_session_factory

_NUM = r"\d+(?:[.,]\d+)?"


def _f(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        return float(s.replace("−", "-").replace(",", "."))
    except ValueError:
        return None


def _strategy(text: str) -> str | None:
    m = re.search(r"Стратегия\s*«([^»]+)»", text)
    return m.group(1) if m else None


def _asset(text: str) -> str | None:
    m = re.search(r"по\s+([A-Za-z0-9]+)\s*·", text)
    return m.group(1) if m else None


def _direction(text: str) -> str | None:
    if "ЛОНГ" in text:
        return "long"
    if "ШОРТ" in text:
        return "short"
    return None


@dataclass
class OpenTrade:
    strategy: str
    asset: str
    direction: str
    entry: float
    stop: float
    target: float


@dataclass
class CloseTrade:
    strategy: str
    asset: str
    direction: str
    outcome: str  # target | stop | reverse
    claimed_r: float
    entry: float
    exit: float


def parse_open(text: str) -> OpenTrade | None:
    m = re.search(
        rf"Вход\s*({_NUM}),\s*стоп\s*({_NUM}),\s*цель\s*({_NUM})", text, re.IGNORECASE
    )
    s, a, d = _strategy(text), _asset(text), _direction(text)
    if not (m and s and a and d):
        return None
    return OpenTrade(s, a, d, _f(m.group(1)), _f(m.group(2)), _f(m.group(3)))


def parse_close(text: str) -> CloseTrade | None:
    m = re.search(
        rf"Результат:\s*([+\-−]?{_NUM})\s*R\s*\(вход\s*({_NUM})\s*→\s*выход\s*({_NUM})\)",
        text,
        re.IGNORECASE,
    )
    s, a, d = _strategy(text), _asset(text), _direction(text)
    if not (m and s and a and d):
        return None
    outcome = "target" if "цель достигнута" in text else "stop" if "поймала стоп" in text else "reverse"
    return CloseTrade(s, a, d, outcome, _f(m.group(1)), _f(m.group(2)), _f(m.group(3)))


def implied_r(entry: float, exit: float, stop: float, direction: str) -> float | None:
    risk = abs(entry - stop)
    if risk == 0:
        return None
    move = (exit - entry) if direction == "long" else (entry - exit)
    return move / risk


@dataclass
class BotScore:
    strategy: str
    closes: int = 0
    paired: int = 0
    r_consistent: int = 0
    r_mismatch: int = 0
    exit_level_ok: int = 0  # target-close exits near target / stop-close near stop
    exit_level_bad: int = 0
    mismatches: list[str] = field(default_factory=list)


def run(chat_id: int) -> dict[str, BotScore]:
    settings = load_settings()
    engine = create_db_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        texts = list(
            session.scalars(
                select(Message.text).where(
                    Message.chat_id == chat_id, Message.text.like("%Стратегия%")
                )
            )
        )

    opens: dict[tuple, OpenTrade] = {}
    closes: list[CloseTrade] = []
    for t in texts:
        if not t:
            continue
        if "открыла" in t:
            o = parse_open(t)
            if o:
                opens[(o.strategy, o.asset, round(o.entry, 8))] = o
        elif "закрыла" in t:
            cl = parse_close(t)
            if cl:
                closes.append(cl)

    scores: dict[str, BotScore] = defaultdict(lambda: BotScore(strategy="?"))
    for cl in closes:
        s = scores[cl.strategy]
        s.strategy = cl.strategy
        s.closes += 1
        o = opens.get((cl.strategy, cl.asset, round(cl.entry, 8)))
        if not o:
            continue
        s.paired += 1
        imp = implied_r(o.entry, cl.exit, o.stop, o.direction)
        if imp is None:
            continue
        if abs(imp - cl.claimed_r) <= 0.25:
            s.r_consistent += 1
        else:
            s.r_mismatch += 1
            if len(s.mismatches) < 5:
                s.mismatches.append(
                    f"{cl.asset} {cl.direction} {cl.outcome}: claimed {cl.claimed_r:+}R "
                    f"but entry {o.entry}->exit {cl.exit} (stop {o.stop}) implies {imp:+.2f}R"
                )
        tol = abs(o.entry - o.stop) * 0.5
        if cl.outcome == "target" and abs(cl.exit - o.target) <= tol:
            s.exit_level_ok += 1
        elif cl.outcome == "stop" and abs(cl.exit - o.stop) <= tol:
            s.exit_level_ok += 1
        elif cl.outcome in ("target", "stop"):
            s.exit_level_bad += 1
    return dict(scores)


def render(scores: dict[str, BotScore]) -> str:
    lines = ["# Strategy-bot self-consistency check", ""]
    lines.append("| strategy | closes | paired | R-consistent | R-mismatch | exit-level ok/bad |")
    lines.append("|---|--:|--:|--:|--:|--:|")
    for s in sorted(scores.values(), key=lambda x: -x.closes):
        lines.append(
            f"| {s.strategy} | {s.closes} | {s.paired} | {s.r_consistent} | "
            f"{s.r_mismatch} | {s.exit_level_ok}/{s.exit_level_bad} |"
        )
    lines.append("")
    any_mm = [m for s in scores.values() for m in s.mismatches]
    if any_mm:
        lines.append("### Sample R mismatches")
        for m in any_mm[:12]:
            lines.append(f"- {m}")
    lines.append("")
    lines.append(
        "_R-consistent = the claimed ±R matches the R implied by the stated "
        "entry/exit/stop (±0.25R). 'paired' < 'closes' when the matching OPEN is "
        "outside the captured window. This checks the bots' INTERNAL honesty; a "
        "separate price check (real candles) confirms the levels actually traded._"
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify strategy-bot self-consistency.")
    parser.add_argument("--chat", type=int, required=True)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    report = render(run(args.chat))
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
        print(f"Wrote {args.out}")
    else:
        print(report)


if __name__ == "__main__":
    main()
