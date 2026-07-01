"""Gather CONCRETE, quotable evidence of manipulation for one channel.

Unlike the scorecard (summary numbers), this pulls the actual message text and
ids behind each finding so an agent can walk the user through them one by one,
in plain language: "here is exactly what they posted, and here is why it's a
trick." Deterministic (no LLM) — the agent narrates the evidence this returns.

Format-specific detection is driven by the channel's profile (see channels.py);
deletions and edits are generic and run for any channel. For channels without a
profile only the generic checks apply.

Usage:
    python -m tgpars.analysis.explain --chat -100...
"""

from __future__ import annotations

import argparse
import re
from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..channels import ChannelProfile, get_profile
from ..config import load_settings
from ..db.models import Message
from ..db.session import create_db_engine, init_db, make_session_factory
from . import metrics

_TARGET_LINE = re.compile(r"Target\s*\d+:", re.IGNORECASE)
# Break-even / stop-to-BE phrases (used to turn losses into "flat" outcomes).
_BREAKEVEN = re.compile(r"б/у|\bбу\b|безубыт|в\s*бу|перевожу в", re.IGNORECASE)


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


# --- GENERIC checks (work for any channel; no format knowledge) --------------


def find_captured_deletions(messages: list[Message]) -> list[dict]:
    """Posts that were deleted but we snapshotted first — the smoking gun."""
    out = []
    for m in messages:
        if m.is_deleted and m.text:
            out.append(
                {
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
            out.append({"message_id": m.message_id, "before": before, "after": after})
    return out[:8]


# --- CHANNEL-SPECIFIC checks (need a profile) --------------------------------


def find_leverage_asymmetry(messages: list[Message], profile: ChannelProfile) -> list[dict]:
    """Wins quoted at one leverage, losses at another — with a constant-leverage
    recompute showing the green feed only exists because of the switch."""
    if not (profile.lev_profit and profile.lev_loss):
        return []
    win_levs, loss_levs = [], []
    win_tp1_spot, loss_spot = [], []
    win_ex = loss_ex = None
    for m in messages:
        if not m.text:
            continue
        pw = profile.lev_profit.search(m.text)
        pl = profile.lev_loss.search(m.text)
        if pw:
            lev = int(pw.group(2))
            win_levs.append(lev)
            if len(_TARGET_LINE.findall(m.text)) == 1:
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
        return []
    fact = {
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
        hi = max(win_set)
        fact["recompute"] = {
            "avg_tp1_spot": round(avg_win, 2),
            "avg_stop_spot": round(avg_loss, 2),
            "reported_win": round(avg_win * hi, 1),
            "reported_loss": round(avg_loss * min(loss_set), 1),
            "honest_hi_loss": round(avg_loss * hi, 1),
            "stops_per_win": round((avg_loss * hi) / (avg_win * hi), 1),
        }
    return [fact]


def find_trader_tricks(
    messages: list[Message], tags: dict[int, str | None], scores: dict
) -> list[dict]:
    """Per-sub-trader breakdown — each trader has a different style/trick."""
    by_tag: dict[str, list[Message]] = defaultdict(list)
    for m in messages:
        t = tags.get(m.message_id)
        if t and not t.startswith("strat:") and t != "unknown":
            by_tag[t].append(m)

    out = []
    for tag, ms in by_tag.items():
        s = scores.get(tag)
        if s is None or s.signals < 3:
            continue
        blob = " ".join(m.text or "" for m in ms)
        reports_r = bool(re.search(r"[+\-−]\d+([.,]\d+)?\s*R\b", blob))
        be_moves = len(_BREAKEVEN.findall(blob))
        uses_lev = bool(re.search(r"(Profit|Loss)\s*\(\d+x\)", blob, re.IGNORECASE))
        be_ratio = round(be_moves / max(s.signals, 1), 2)

        if uses_lev:
            trick = "quotes wins/losses at different leverage (see leverage fact)"
        elif be_ratio >= 0.8:
            trick = (
                f"heavy break-even: {be_moves} 'stop to BE' moves over {s.signals} "
                "signals — losses become 'flat', not losses"
            )
        elif not reports_r:
            trick = "no R accountability; frames outcomes as 'potential profit' / break-even"
        elif s.cancel_rate and s.cancel_rate >= 25:
            trick = f"{s.cancel_rate}% of signals cancelled (losing setups dropped)"
        else:
            trick = "relatively straightforward reporting (reports R)"

        out.append(
            {
                "tag": tag,
                "signals": s.signals,
                "claimed_wr": s.claimed_winrate,
                "sum_r": s.total_r,
                "cancels": s.cancels,
                "cancel_rate": s.cancel_rate,
                "breakeven_moves": be_moves,
                "reports_r": reports_r,
                "trick": trick,
            }
        )
    return sorted(out, key=lambda d: -d["signals"])


# --- assembly ----------------------------------------------------------------


def gather(session: Session, chat_id: int) -> dict:
    profile = get_profile(chat_id)
    messages = _load(session, chat_id)
    scores = metrics.score_chat(session, chat_id)
    tags = metrics._resolve_tags(messages)

    # Scorecard-derived facts rely on the keyword classifier, which is only
    # calibrated for channels with a known format. For unprofiled channels
    # (e.g. discussion chats) these would be noise, so gate them.
    high_cancel, negative_r = [], []
    if profile.has_format:
        high_cancel = [
            (tag, s.cancel_rate, s.cancels, s.signals)
            for tag, s in scores.items()
            if s.cancel_rate is not None and s.cancel_rate >= 25 and not tag.startswith("strat:")
        ]
        negative_r = [
            (tag, s.total_r)
            for tag, s in scores.items()
            if s.total_r is not None and s.total_r < 0
        ]
    return {
        "chat_id": chat_id,
        "profile": profile.name,
        "profile_notes": list(profile.notes),
        "total_messages": len(messages),
        # generic
        "captured_deletions": find_captured_deletions(messages),
        "edits": find_edits(messages),
        # channel-specific (empty if no profile)
        "leverage_asymmetry": find_leverage_asymmetry(messages, profile),
        "trader_tricks": find_trader_tricks(messages, tags, scores) if profile.has_trader_tags else [],
        "high_cancel": sorted(high_cancel, key=lambda x: -x[1]),
        "negative_sum_r": sorted(negative_r, key=lambda x: x[1]),
    }


def render(ev: dict) -> str:
    L = [f"# Evidence pack — chat {ev['chat_id']} ({ev['total_messages']} messages)"]
    L.append(f"channel profile: **{ev['profile']}**"
             + ("" if ev["profile"] != "generic" else "  (no format profile — generic checks only)"))
    L.append("\nWalk the user through each item ONE AT A TIME, in plain language, quoting the "
             "real post. Do not dump this as a table.\n")

    if ev["profile_notes"]:
        L.append("Channel quirks: " + " ".join(f"({i+1}) {n}" for i, n in enumerate(ev["profile_notes"])) + "\n")

    n = 0
    if ev["leverage_asymmetry"]:
        a = ev["leverage_asymmetry"][0]
        n += 1
        L.append(f"## FACT {n} — The leverage switch (strongest, provable)")
        allw = "ALL" if a.get("win_all_same") else "most"
        alll = "ALL" if a.get("loss_all_same") else "most"
        L.append(f"{allw} {a['win_count']} winning posts at {a['win_leverage']}x; {alll} "
                 f"{a['loss_count']} losing posts at {a['loss_leverage']}x. Never a loss at the win leverage.")
        L.append(f"- WIN (msg {a['win_example'][0]}): \"{a['win_example'][1]}\"")
        L.append(f"- LOSS (msg {a['loss_example'][0]}): \"{a['loss_example'][1]}\"")
        r = a.get("recompute")
        if r:
            L.append(f"Math: avg +{r['avg_tp1_spot']}% price move to TP1 vs -{r['avg_stop_spot']}% to "
                     f"the stop (stop ~2x further). At their own win leverage on BOTH, each stop is "
                     f"-{r['honest_hi_loss']}% = erases {r['stops_per_win']} TP1 wins. Green feed exists "
                     "ONLY because of the switch.")
        L.append("")

    if ev["trader_tricks"]:
        n += 1
        L.append(f"## FACT {n} — Each sub-trader has a DIFFERENT trick")
        for t in ev["trader_tricks"]:
            wr = f"{t['claimed_wr']}%" if t["claimed_wr"] is not None else "—"
            sr = f"{t['sum_r']}R" if t["sum_r"] is not None else "—"
            L.append(f"- trader {t['tag']}: {t['signals']} signals, claimed WR {wr}, sumR {sr} → "
                     f"{t['trick']}")
        L.append("Narrate the 2-3 most distinct ones (e.g. break-even abuse vs no-R vs cancels).\n")

    if ev["high_cancel"]:
        n += 1
        L.append(f"## FACT {n} — Selectively cancelled signals")
        for tag, rate, cancels, sigs in ev["high_cancel"]:
            L.append(f"- trader {tag}: {rate}% cancelled ({cancels} cancels vs {sigs} signals)")
        L.append("An unfilled limit can't become a loss; cancelling 'it just missed us' keeps losers out.\n")

    if ev["negative_sum_r"]:
        n += 1
        L.append(f"## FACT {n} — Net-losing on sum-of-R (win-rate hides it)")
        for tag, r in ev["negative_sum_r"]:
            L.append(f"- {tag}: total {r}R (negative = net losing)")
        L.append("")

    if ev["captured_deletions"]:
        n += 1
        L.append(f"## FACT {n} — Posts they DELETED but we saved first (generic, strongest proof)")
        for d in ev["captured_deletions"]:
            L.append(f"- msg {d['message_id']} posted {d['posted_at']}, deleted {d['deleted_at']}:")
            L.append(f'  "{d["text"]}"')
        L.append("")

    if ev["edits"]:
        n += 1
        L.append(f"## FACT {n} — Edited-after-the-fact posts (generic)")
        for e in ev["edits"]:
            L.append(f"- msg {e['message_id']}: BEFORE \"{e['before']}\"  →  AFTER \"{e['after']}\"")
        L.append("Most edits are cosmetic (typos); flag any that change reported numbers/stats.\n")

    if n == 0:
        L.append("No manipulation patterns detected in the captured data for this channel.")
    return "\n".join(L)


def main() -> None:
    parser = argparse.ArgumentParser(description="Concrete manipulation evidence for a channel.")
    parser.add_argument("--chat", type=int, required=True)
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    settings = load_settings()
    engine = create_db_engine(settings.database_url)
    init_db(engine)
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
