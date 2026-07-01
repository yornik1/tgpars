"""Grade a discussion-chat forecaster's free-form calls against real price.

Some monitored chats have no structured signals (entry/stop/target), just a
resident "analyst" who posts prose BTC forecasts. There is nothing for the
signal verifier to check, but the calls still carry a *direction* on a short
horizon — and that we CAN grade against exchange OHLCV.

For the configured `forecaster` sender we:
  1. pull their forecast posts,
  2. classify the near-term (1-2 day) direction from the text (up/down/none),
  3. fetch real daily BTC candles and measure the actual move over the horizon,
  4. score HIT / miss and report a real hit-rate vs the ~50% coin-flip baseline.

Plus an ID-gap audit: how many message_ids are absent from our capture. This is
NOT a deletion count — gaps also cover service messages, filtered senders, and
collector downtime — so it is reported as *candidates*, with the live-detected
deletion count alongside as the only proven figure.

Deterministic; no LLM. Usage:
    python -m tgpars.analysis.forecast_scorecard --chat -100...
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..channels import get_profile
from ..config import load_settings
from ..db.models import Message
from ..db.session import create_db_engine, init_db, make_session_factory

# Direction lexicon for the near-term segment of a forecast.
_BULL = ("отскок", "рост", "восстанов", "вверх", "бычий", "подъём", "подъем",
         "покупк", "ралли", "возобновл", "укреплен", "выше")
_BEAR = ("коррекц", "снижен", "падени", "вниз", "медвежий", "распродаж",
         "откат", "просадк", "слабост", "ниже", "обвал")

_NEAR = re.compile(r"(1[–\-]?2\s*дн|краткосроч|ближайш|завтра|сутки)", re.I)
# Posts that read as forecasts and reference BTC / a price level.
_IS_BTC = ("btc", "биткои", "тыс", "$1", "$9", "$8")


def _near_term_direction(text: str) -> str | None:
    """up / down / None from the short-horizon part of a forecast post."""
    seg = text
    m = _NEAR.search(text)
    if m:
        seg = text[m.start(): m.start() + 400]
    low = seg.lower()
    b = sum(low.count(w) for w in _BULL)
    r = sum(low.count(w) for w in _BEAR)
    if b > r:
        return "up"
    if r > b:
        return "down"
    return None


def audit_id_gaps(session: Session, chat_id: int) -> dict:
    ids = list(
        session.scalars(
            select(Message.message_id).where(Message.chat_id == chat_id).order_by(Message.message_id)
        )
    )
    if not ids:
        return {"stored": 0}
    lo, hi = ids[0], ids[-1]
    deleted_count = len(
        list(
            session.scalars(
                select(Message.message_id).where(
                    Message.chat_id == chat_id, Message.is_deleted.is_(True)
                )
            )
        )
    )
    return {
        "low": lo,
        "high": hi,
        "span": hi - lo + 1,
        "stored": len(ids),
        "missing_ids": (hi - lo + 1) - len(ids),
        "live_deletions": deleted_count,
    }


def _load_forecasts(session: Session, chat_id: int, sender_id: int) -> list[Message]:
    msgs = list(
        session.scalars(
            select(Message)
            .where(
                Message.chat_id == chat_id,
                Message.sender_id == sender_id,
                Message.text.is_not(None),
            )
            .order_by(Message.posted_at)
        )
    )
    out = []
    for m in msgs:
        t = m.text or ""
        if "рогноз" not in t:
            continue
        low = t.lower()
        if not any(k in low for k in _IS_BTC):
            continue
        out.append(m)
    return out


def _daily_closes(since: datetime, symbol: str = "BTC/USDT") -> dict[str, float]:
    """{yyyy-mm-dd: close} of daily candles from `since` forward. Needs ccxt."""
    import ccxt

    ex = ccxt.binance()
    cursor = int(since.timestamp() * 1000)
    closes: dict[str, float] = {}
    while True:
        batch = ex.fetch_ohlcv(symbol, "1d", since=cursor, limit=1000)
        if not batch:
            break
        for ts, _o, _h, _l, cl, _v in batch:
            ds = datetime.fromtimestamp(ts / 1000, timezone.utc).strftime("%Y-%m-%d")
            closes[ds] = cl
        cursor = batch[-1][0] + 86_400_000
        if len(batch) < 1000:
            break
    return closes


def _close_near(closes: dict[str, float], dt: datetime) -> float | None:
    for k in range(3):  # tolerate up to 2 missing days
        ds = (dt + timedelta(days=k)).strftime("%Y-%m-%d")
        if ds in closes:
            return closes[ds]
    return None


def score(session: Session, chat_id: int, *, horizon_days: int = 2, threshold: float = 0.005) -> dict:
    profile = get_profile(chat_id)
    if profile.forecaster is None:
        return {"error": "no forecaster configured for this chat"}

    posts = _load_forecasts(session, chat_id, profile.forecaster)
    graded = []
    for m in posts:
        d = _near_term_direction(m.text or "")
        if d is None:
            continue
        dt = m.posted_at.replace(tzinfo=timezone.utc) if m.posted_at.tzinfo is None else m.posted_at
        graded.append((m.message_id, dt, d))

    result: dict = {
        "forecaster": profile.forecaster,
        "horizon_days": horizon_days,
        "threshold_pct": threshold * 100,
        "forecast_posts": len(posts),
        "directional": len(graded),
        "rows": [],
        "hits": 0,
        "misses": 0,
        "flat": 0,
    }
    if not graded:
        return result

    closes = _daily_closes(min(g[1] for g in graded) - timedelta(days=2))
    for mid, dt, d in graded:
        p0 = _close_near(closes, dt)
        p1 = _close_near(closes, dt + timedelta(days=horizon_days))
        if p0 is None or p1 is None:
            continue
        chg = (p1 - p0) / p0
        if abs(chg) < threshold:
            verdict = "flat"
            result["flat"] += 1
        elif (chg > 0 and d == "up") or (chg < 0 and d == "down"):
            verdict = "HIT"
            result["hits"] += 1
        else:
            verdict = "miss"
            result["misses"] += 1
        result["rows"].append(
            {"date": dt.strftime("%Y-%m-%d"), "msg": mid, "pred": d,
             "moved_pct": round(chg * 100, 1), "verdict": verdict}
        )
    return result


def render(chat_id: int, gaps: dict, sc: dict) -> str:
    L = [f"# Forecast scorecard — chat {chat_id}", ""]

    L.append("## ID-gap audit (deletions)")
    if gaps.get("stored"):
        L.append(f"- ids {gaps['low']}..{gaps['high']} (span {gaps['span']}), stored {gaps['stored']}")
        L.append(f"- **{gaps['missing_ids']} missing ids** — CANDIDATES only, not proven deletions "
                 "(service msgs, filtered senders, collector downtime also leave gaps)")
        L.append(f"- **{gaps['live_deletions']} proven deletions** (live-captured, is_deleted=1) — "
                 "the only reliable figure; grows only while the daemon runs")
    else:
        L.append("- no messages stored")
    L.append("")

    if "error" in sc:
        L.append(f"## Forecaster grading\n- {sc['error']}")
        return "\n".join(L)

    scored = sc["hits"] + sc["misses"]
    L.append(f"## Forecaster grading (sender {sc['forecaster']}, {sc['horizon_days']}-day horizon)")
    L.append(f"- {sc['forecast_posts']} forecast posts, {sc['directional']} with a clear direction, "
             f"{scored} gradable ({sc['flat']} flat/ignored, |move| < {sc['threshold_pct']:.1f}%)")
    for r in sc["rows"]:
        L.append(f"  {r['date']}  m{r['msg']:<6} pred={r['pred']:<4} moved={r['moved_pct']:+.1f}%  -> {r['verdict']}")
    if scored:
        wr = sc["hits"] / scored * 100
        L.append(f"- **REAL short-term hit-rate = {sc['hits']}/{scored} = {wr:.0f}%** "
                 "(coin-flip baseline ~50%)")
    L.append("")
    L.append("Caveat: direction is keyword-classified from prose and same-day duplicate posts count "
             "separately; treat this as an approximate radar, not a P&L.")
    return "\n".join(L)


def main() -> None:
    parser = argparse.ArgumentParser(description="Grade a chat forecaster against real BTC price.")
    parser.add_argument("--chat", type=int, required=True)
    parser.add_argument("--horizon", type=int, default=2, help="grading horizon in days")
    args = parser.parse_args()

    settings = load_settings()
    engine = create_db_engine(settings.database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        gaps = audit_id_gaps(session, args.chat)
        sc = score(session, args.chat, horizon_days=args.horizon)
    print(render(args.chat, gaps, sc))


if __name__ == "__main__":
    main()
