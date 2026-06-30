"""Price-verify parsed signals in bulk and compare REAL vs claimed win-rate.

For each signal-classified message it: resolves the sub-trader, regex-parses the
signal, and (if verifiable) checks against real OHLCV whether TP1 was reached
before the stop. Aggregates a price-verified win-rate per trader — the ground
truth to set against the channel's self-reported numbers.

Usage:
    python -m tgpars.analysis.verify_batch --chat -100... [--exchange binance] [--limit N]
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass

import ccxt
from sqlalchemy import select

from ..config import load_settings
from ..db.models import Message
from ..db.session import create_db_engine, make_session_factory
from . import metrics
from .signal_parse import parse_signal
from .verify import verify_signal


@dataclass
class TraderVerify:
    tag: str
    signals: int = 0
    verifiable: int = 0
    tp1: int = 0  # reached first take-profit before stop
    sl: int = 0  # stopped out with no target
    inconclusive: int = 0  # still open / no fill in window

    @property
    def real_winrate(self) -> float | None:
        denom = self.tp1 + self.sl
        return round(self.tp1 / denom * 100, 1) if denom else None


def run(chat_id: int, exchange_id: str = "binance", limit: int | None = None) -> dict[str, TraderVerify]:
    settings = load_settings()
    engine = create_db_engine(settings.database_url)
    session_factory = make_session_factory(engine)

    exchange = getattr(ccxt, exchange_id)({"enableRateLimit": True})
    exchange.load_markets()

    with session_factory() as session:
        messages = list(
            session.scalars(
                select(Message).where(Message.chat_id == chat_id).order_by(Message.posted_at)
            )
        )

    tags = metrics._resolve_tags(messages)
    out: dict[str, TraderVerify] = defaultdict(lambda: TraderVerify(tag="?"))
    done = 0
    for m in messages:
        tag = tags.get(m.message_id) or "unknown"
        if tag.startswith("strat:"):  # bots self-report fills; skip for now
            continue
        if metrics.classify(m.text) != "signal":
            continue
        tv = out[tag]
        tv.tag = tag
        tv.signals += 1

        sig = parse_signal(m.text)
        if not sig.is_verifiable:
            continue
        if limit is not None and done >= limit:
            continue
        try:
            r = verify_signal(
                asset=sig.asset,
                direction=sig.direction,
                posted_at=m.posted_at,
                entry=sig.entry_high,
                stop=sig.stop,
                targets=sig.targets,
                order_type=sig.order_type,
                exchange=exchange,
                timeframe="15m",
            )
        except Exception:  # noqa: BLE001 - network/symbol issues -> not verifiable
            continue
        done += 1
        if not r.verifiable or r.outcome == "no_fill":
            continue
        tv.verifiable += 1
        if r.targets_hit >= 1:
            tv.tp1 += 1
        elif r.sl_hit:
            tv.sl += 1
        else:
            tv.inconclusive += 1
    return dict(out)


def render(results: dict[str, TraderVerify]) -> str:
    lines = ["# Price-verified win-rate (REAL, from exchange OHLCV)", ""]
    lines.append("| trader | signals | verifiable | TP1-first | SL-first | open | REAL WR |")
    lines.append("|---|--:|--:|--:|--:|--:|--:|")
    for tag, tv in sorted(results.items(), key=lambda kv: -kv[1].verifiable):
        if tv.signals == 0:
            continue
        wr = tv.real_winrate
        lines.append(
            f"| {tag} | {tv.signals} | {tv.verifiable} | {tv.tp1} | {tv.sl} | "
            f"{tv.inconclusive} | {wr if wr is not None else '—'} |"
        )
    lines.append("")
    lines.append(
        "_TP1-first = price reached the first take-profit before the stop; SL-first "
        "= stopped out with no target reached. 'verifiable' < 'signals' because many "
        "small alts / forex are not on the exchange (binance spot) or lacked a clean "
        "parse. Compare REAL WR with the claimed WR in the scorecard._"
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description="Price-verify parsed signals in bulk.")
    parser.add_argument("--chat", type=int, required=True)
    parser.add_argument("--exchange", default="binance")
    parser.add_argument("--limit", type=int, default=None, help="cap signals verified")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    results = run(args.chat, exchange_id=args.exchange, limit=args.limit)
    report = render(results)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
        print(f"Wrote {args.out}")
    else:
        print(report)


if __name__ == "__main__":
    main()
