"""Gather CONCRETE, quotable evidence of manipulation for one channel.

Unlike the scorecard (summary numbers), this pulls the actual message text and
ids behind each finding so an agent can walk the user through them one by one,
in plain language: "here is exactly what they posted, and here is why it's a
trick." Deterministic (no LLM) — the agent narrates the evidence this returns.

Usage:
    python -m tgpars.analysis.explain --chat -100...
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import load_settings
from ..db.models import Message
from ..db.session import create_db_engine, init_db, make_session_factory
from . import metrics

_LEV_PROFIT = re.compile(r"Profit\s*\((\d+)x\)", re.IGNORECASE)
_LEV_LOSS = re.compile(r"Loss\s*\((\d+)x\)", re.IGNORECASE)
_PROFIT_PCT = re.compile(r"([\d.,]+)%\s*Profit", re.IGNORECASE)


def _quote(text: str | None, limit: int = 240) -> str:
    if not text:
        return "(no text / media)"
    t = " ".join(text.split())
    return t[:limit] + ("…" if len(t) > limit else "")


def _load(session: Session, chat_id: int) -> list[Message]:
    return list(
        session.scalars(
            select(Message).where(Message.chat_id == chat_id).order_by(Message.posted_at)
        )
    )


def find_leverage_asymmetry(messages: list[Message]) -> list[dict]:
    """Wins quoted at higher leverage than losses — same account, different optics."""
    wins, losses = [], []
    for m in messages:
        if not m.text:
            continue
        pw = _LEV_PROFIT.search(m.text)
        pl = _LEV_LOSS.search(m.text)
        if pw:
            wins.append((int(pw.group(1)), m))
        if pl:
            losses.append((int(pl.group(1)), m))
    if not wins or not losses:
        return []
    win_levs = {w for w, _ in wins}
    loss_levs = {l for l, _ in losses}
    if max(win_levs) <= min(loss_levs):
        return []  # no asymmetry
    return [
        {
            "kind": "leverage_asymmetry",
            "win_leverage": sorted(win_levs),
            "loss_leverage": sorted(loss_levs),
            "win_example": (wins[-1][1].message_id, _quote(wins[-1][1].text)),
            "loss_example": (losses[-1][1].message_id, _quote(losses[-1][1].text)),
            "win_count": len(wins),
            "loss_count": len(losses),
        }
    ]


def find_repost_inflation(messages: list[Message]) -> list[dict]:
    """One trade re-posted several times as its profit grows -> inflated win count."""
    by_ref: dict[str, list[Message]] = defaultdict(list)
    for m in messages:
        if m.text and _LEV_PROFIT.search(m.text):
            ref = metrics.signal_ref(m.text)
            if ref:
                by_ref[ref].append(m)
    out = []
    for ref, msgs in by_ref.items():
        if len(msgs) >= 3:  # same trade posted 3+ times
            pcts = []
            for m in msgs:
                pm = _PROFIT_PCT.search(m.text or "")
                if pm:
                    pcts.append((pm.group(1) + "%", m.message_id, _quote(m.text, 120)))
            out.append({"kind": "repost_inflation", "ref": ref, "times": len(msgs), "posts": pcts})
    return sorted(out, key=lambda d: -d["times"])[:3]


def find_captured_deletions(messages: list[Message]) -> list[dict]:
    """Posts that were deleted but we snapshotted first — the smoking gun."""
    out = []
    for m in messages:
        if m.is_deleted and m.text:
            out.append(
                {
                    "kind": "deleted_post",
                    "message_id": m.message_id,
                    "posted_at": m.posted_at.isoformat() if m.posted_at else "?",
                    "deleted_at": m.deleted_detected_at.isoformat()
                    if m.deleted_detected_at
                    else "?",
                    "text": _quote(m.text, 400),
                }
            )
    return out


def _diff_window(before: str, after: str, pad: int = 60) -> tuple[str, str]:
    """Return the differing region of before/after, so an edit is visible even
    when the change is deep in a long post."""
    b, a = before, after
    i = 0
    while i < min(len(b), len(a)) and b[i] == a[i]:
        i += 1
    start = max(0, i - pad)
    return (
        ("…" if start else "") + _quote(b[start : i + pad], 160),
        ("…" if start else "") + _quote(a[start : i + pad], 160),
    )


def find_edits(messages: list[Message]) -> list[dict]:
    out = []
    for m in messages:
        if m.edit_count and m.original_text and m.text and m.original_text != m.text:
            before, after = _diff_window(m.original_text, m.text)
            out.append(
                {
                    "kind": "edited_post",
                    "message_id": m.message_id,
                    "before": before,
                    "after": after,
                }
            )
    return out[:5]


def gather(session: Session, chat_id: int) -> dict:
    messages = _load(session, chat_id)
    scores = metrics.score_chat(session, chat_id)
    # traders with high cancel rate
    high_cancel = [
        (tag, s.cancel_rate, s.cancels, s.signals)
        for tag, s in scores.items()
        if s.cancel_rate is not None and s.cancel_rate >= 25 and not tag.startswith("strat:")
    ]
    # negative sum-R buckets
    negative_r = [
        (tag, s.total_r) for tag, s in scores.items() if s.total_r is not None and s.total_r < 0
    ]
    return {
        "chat_id": chat_id,
        "total_messages": len(messages),
        "leverage_asymmetry": find_leverage_asymmetry(messages),
        "repost_inflation": find_repost_inflation(messages),
        "high_cancel": sorted(high_cancel, key=lambda x: -x[1]),
        "negative_sum_r": sorted(negative_r, key=lambda x: x[1]),
        "captured_deletions": find_captured_deletions(messages),
        "edits": find_edits(messages),
        "traders": {
            tag: {
                "signals": s.signals,
                "claimed_wr": s.claimed_winrate,
                "sum_r": s.total_r,
                "wins": s.wins,
                "unique_wins": s.unique_wins,
                "losses": s.losses,
                "cancels": s.cancels,
            }
            for tag, s in scores.items()
        },
    }


def render(ev: dict) -> str:
    """Human-narratable evidence pack (the agent expands each into plain language)."""
    L = [f"# Evidence pack — chat {ev['chat_id']} ({ev['total_messages']} messages)", ""]
    L.append("Walk the user through each item below ONE AT A TIME, in plain language, "
             "quoting the real post. Do not dump this as a table.\n")

    if ev["leverage_asymmetry"]:
        a = ev["leverage_asymmetry"][0]
        L.append("## FACT 1 — Leverage asymmetry (wins vs losses counted differently)")
        L.append(f"Wins are quoted at leverage {a['win_leverage']}, losses at {a['loss_leverage']}.")
        L.append(f"- WIN post (msg {a['win_example'][0]}): \"{a['win_example'][1]}\"")
        L.append(f"- LOSS post (msg {a['loss_example'][0]}): \"{a['loss_example'][1]}\"")
        L.append("Why it's a trick: same account, but profit is shown at a higher multiplier "
                 "than loss, so the feed looks far greener than the real result.\n")

    if ev["repost_inflation"]:
        L.append("## FACT 2 — One trade re-posted as growing profit (win-count inflation)")
        for r in ev["repost_inflation"]:
            posts = " → ".join(p[0] for p in r["posts"]) or f"{r['times']} times"
            L.append(f"- Trade {r['ref']} posted {r['times']} times: {posts} "
                     f"(msgs {[p[1] for p in r['posts']]})")
        L.append("Why it's a trick: it's ONE trade, but each re-post reads as another 'win'.\n")

    if ev["high_cancel"]:
        L.append("## FACT 3 — Selectively cancelled signals (losing setups quietly dropped)")
        for tag, rate, cancels, sigs in ev["high_cancel"]:
            L.append(f"- trader {tag}: {rate}% cancelled ({cancels} cancels vs {sigs} signals)")
        L.append("Why it's a trick: an unfilled limit order can't become a loss, so cancelling "
                 "'it just missed us' setups keeps losers out of the record.\n")

    if ev["negative_sum_r"]:
        L.append("## FACT 4 — Actually losing on sum-of-R (win-rate hides it)")
        for tag, r in ev["negative_sum_r"]:
            L.append(f"- {tag}: total {r}R over the period (negative = net losing)")
        L.append("Why it matters: counting 'wins' by quantity looks OK, but summing R shows "
                 "the money reality — these are net losers.\n")

    if ev["captured_deletions"]:
        L.append("## FACT 5 — Posts they DELETED but we saved first")
        for d in ev["captured_deletions"]:
            L.append(f"- msg {d['message_id']} posted {d['posted_at']}, deleted {d['deleted_at']}:")
            L.append(f'  "{d["text"]}"')
        L.append("Why it matters: deletions only survive because we snapshot live — this is "
                 "direct proof of what they removed.\n")

    if ev["edits"]:
        L.append("## FACT 6 — Edited-after-the-fact posts")
        for e in ev["edits"]:
            L.append(f"- msg {e['message_id']}: BEFORE \"{e['before']}\"  →  AFTER \"{e['after']}\"")
        L.append("")

    if not any([ev["leverage_asymmetry"], ev["repost_inflation"], ev["high_cancel"],
                ev["negative_sum_r"], ev["captured_deletions"], ev["edits"]]):
        L.append("No manipulation patterns detected in the captured data for this channel.")
    return "\n".join(L)


def main() -> None:
    parser = argparse.ArgumentParser(description="Concrete manipulation evidence for a channel.")
    parser.add_argument("--chat", type=int, required=True)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    settings = load_settings()
    engine = create_db_engine(settings.database_url)
    init_db(engine)  # idempotent: applies lightweight column migrations if needed
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        ev = gather(session, args.chat)
    report = render(ev)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
        print(f"Wrote {args.out}")
    else:
        print(report)


if __name__ == "__main__":
    main()
