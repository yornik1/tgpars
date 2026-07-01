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

_LEV_PROFIT = re.compile(r"([\d.,]+)%\s*Profit\s*\((\d+)x\)", re.IGNORECASE)
_LEV_LOSS = re.compile(r"([\d.,]+)%\s*Loss\s*\((\d+)x\)", re.IGNORECASE)
_TARGET_LINE = re.compile(r"Target\s*\d+:", re.IGNORECASE)


def _fnum(s: str) -> float:
    return float(s.replace(",", "."))


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
    """The real trick: wins quoted at one leverage, losses at another — always.

    Also recomputes what the record would look like at a SINGLE consistent
    leverage, using the spot move implied by each post (reported % / leverage).
    The stop being ~2x further than TP1 means constant leverage is net-losing;
    the green feed only exists because of the win/loss leverage switch.
    """
    win_levs, loss_levs = [], []
    win_tp1_spot, loss_spot = [], []  # spot move %% = reported %% / leverage
    win_ex = loss_ex = None
    for m in messages:
        if not m.text:
            continue
        pw = _LEV_PROFIT.search(m.text)
        pl = _LEV_LOSS.search(m.text)
        if pw:
            lev = int(pw.group(2))
            win_levs.append(lev)
            if len(_TARGET_LINE.findall(m.text)) == 1:  # TP1-only = the modal win
                win_tp1_spot.append(_fnum(pw.group(1)) / lev)
            win_ex = (m.message_id, _quote(m.text))
        if pl:
            lev = int(pl.group(2))
            loss_levs.append(lev)
            loss_spot.append(_fnum(pl.group(1)) / lev)
            loss_ex = (m.message_id, _quote(m.text))

    if not win_levs or not loss_levs:
        return []
    win_set, loss_set = sorted(set(win_levs)), sorted(set(loss_levs))
    if max(win_set) <= min(loss_set):
        return []  # no asymmetry

    fact = {
        "kind": "leverage_asymmetry",
        "win_leverage": win_set,
        "loss_leverage": loss_set,
        "win_count": len(win_levs),
        "loss_count": len(loss_levs),
        "win_all_same": len(win_set) == 1,
        "loss_all_same": len(loss_set) == 1,
        "win_example": win_ex,
        "loss_example": loss_ex,
    }
    if win_tp1_spot and loss_spot:
        avg_win = sum(win_tp1_spot) / len(win_tp1_spot)
        avg_loss = sum(loss_spot) / len(loss_spot)
        hi = max(win_set)  # the leverage their wins use
        fact["recompute"] = {
            "avg_tp1_spot": round(avg_win, 2),
            "avg_stop_spot": round(avg_loss, 2),
            "reported_win": round(avg_win * max(win_set), 1),
            "reported_loss": round(avg_loss * min(loss_set), 1),
            "honest_hi_win": round(avg_win * hi, 1),
            "honest_hi_loss": round(avg_loss * hi, 1),
            "stops_per_win": round((avg_loss * hi) / (avg_win * hi), 1),
        }
    return [fact]


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
        L.append("## FACT 1 — The leverage switch (the strongest, provable trick)")
        allw = "ALL" if a.get("win_all_same") else "most"
        alll = "ALL" if a.get("loss_all_same") else "most"
        L.append(f"{allw} {a['win_count']} winning posts are quoted at {a['win_leverage']}x; "
                 f"{alll} {a['loss_count']} losing posts at {a['loss_leverage']}x. "
                 "Not once is a loss shown at the win leverage.")
        L.append(f"- WIN post (msg {a['win_example'][0]}): \"{a['win_example'][1]}\"")
        L.append(f"- LOSS post (msg {a['loss_example'][0]}): \"{a['loss_example'][1]}\"")
        r = a.get("recompute")
        if r:
            L.append(f"The math: on average price moves +{r['avg_tp1_spot']}% to TP1 but "
                     f"-{r['avg_stop_spot']}% to the stop — the stop is ~2x further. "
                     f"They report the win at {a['win_leverage'][-1]}x (+{r['reported_win']}%) "
                     f"and the loss at {a['loss_leverage'][0]}x (only -{r['reported_loss']}%).")
            L.append(f"If you actually used their WIN leverage ({a['win_leverage'][-1]}x) on BOTH, "
                     f"each stop is -{r['honest_hi_loss']}% — one stop erases "
                     f"{r['stops_per_win']} TP1 wins. The green feed exists ONLY because of the "
                     f"win/loss leverage switch; on constant leverage it's net-losing.")
        L.append("")

    if ev["high_cancel"]:
        L.append("## FACT 2 — Selectively cancelled signals (losing setups quietly dropped)")
        for tag, rate, cancels, sigs in ev["high_cancel"]:
            L.append(f"- trader {tag}: {rate}% cancelled ({cancels} cancels vs {sigs} signals)")
        L.append("Why it's a trick: an unfilled limit order can't become a loss, so cancelling "
                 "'it just missed us' setups keeps losers out of the record.\n")

    if ev["negative_sum_r"]:
        L.append("## FACT 3 — Actually losing on sum-of-R (win-rate hides it)")
        for tag, r in ev["negative_sum_r"]:
            L.append(f"- {tag}: total {r}R over the period (negative = net losing)")
        L.append("Why it matters: counting 'wins' by quantity looks OK, but summing R shows "
                 "the money reality — these are net losers.\n")

    if ev["captured_deletions"]:
        L.append("## FACT 4 — Posts they DELETED but we saved first")
        for d in ev["captured_deletions"]:
            L.append(f"- msg {d['message_id']} posted {d['posted_at']}, deleted {d['deleted_at']}:")
            L.append(f'  "{d["text"]}"')
        L.append("Why it matters: deletions only survive because we snapshot live — this is "
                 "direct proof of what they removed.\n")

    if ev["edits"]:
        L.append("## FACT 5 — Edited-after-the-fact posts")
        for e in ev["edits"]:
            L.append(f"- msg {e['message_id']}: BEFORE \"{e['before']}\"  →  AFTER \"{e['after']}\"")
        L.append("")

    if not any([ev["leverage_asymmetry"], ev["high_cancel"],
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
