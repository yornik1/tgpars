"""Per-trader manipulation scorecard (pure code, no LLM).

Reads collected messages, buckets them by sub-trader tag, classifies each with
transparent keyword heuristics, and aggregates the signals that matter for
catching dishonest signallers:

  * claimed win/loss/cancel counts and claimed win-rate
  * cancel-rate (selectively dropping losing limit orders inflates the record)
  * leverage asymmetry (e.g. wins quoted at 5x, losses at 2x)
  * deletion / edit rates (needs the live daemon to be meaningful)

Heuristics are intentionally simple and auditable; an in-session LLM pass can
refine edge cases. This gives a fast, repeatable first-pass verdict.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import Message

# Sub-trader tag. The source spells it "trader", "tarder" (typo) and sometimes
# drops the opening bracket ("trader #c]"). Match the word loosely and capture
# the token after it. e.g. "[trader #c]" -> c, "[trader#3]" -> 3.
_TRADER_BRACKET = re.compile(r"t(?:rader|arder|erder)\s*#?\s*([A-Za-z0-9]+)", re.IGNORECASE)
# "Signal ID: #c12" -> c12 ; "Signal: #f10" -> f10 ; "📍SIGNAL ID: #2170📍" -> 2170
_SIGNAL_REF = re.compile(r"signal\s*id[:\s]*#([A-Za-z]*\d+)", re.IGNORECASE)
_SIGNAL_F = re.compile(r"signal[:\s]*#([A-Za-z]\d+)", re.IGNORECASE)
_LEV_PROFIT = re.compile(r"Profit\s*\((\d+)x\)", re.IGNORECASE)
_LEV_LOSS = re.compile(r"Loss\s*\((\d+)x\)", re.IGNORECASE)

_CANCEL_KW = ("отмен", "инвалид", "не актуально", "без нас", "не дотян", "ушло без", "ушла без")
_LOSS_KW = ("стоп", "loss", "минус", "по стопу", "сработал стоп")
_WIN_KW = ("тейк", "profit", "прибыл", "закрыли оба", "закрыла +", "закрыл +", "✅")


def trader_tag(text: str | None) -> str | None:
    """Best-effort sub-trader id from a message."""
    if not text:
        return None
    m = _TRADER_BRACKET.search(text)
    if m:
        return _normalize_tag(m.group(1))
    m = _SIGNAL_REF.search(text)
    if m:  # "#c12" -> c ; "#2170" -> 3 (trader#3's numeric ids)
        return _normalize_tag(m.group(1))
    m = _SIGNAL_F.search(text)
    if m:  # "Signal: #f10" -> f
        return _normalize_tag(m.group(1))
    if "SIGNAL ID" in text.upper() and re.search(r"#\d{3,}", text):
        return "3"
    return None


def _normalize_tag(token: str) -> str:
    """Strip trailing signal number: 'c12' -> 'c', 'f10' -> 'f'. Pure-numeric
    ids (e.g. '2170') belong to trader #3."""
    letters = re.sub(r"\d+$", "", token).lower()
    return letters or "3"


def _has_neg_r(text: str) -> bool:
    return bool(re.search(r"-\d+([.,]\d+)?\s*R", text)) or bool(
        re.search(r"минус\s*\d", text.lower())
    )


def _has_pos_r(text: str) -> bool:
    return bool(re.search(r"\+\d+([.,]\d+)?\s*R", text)) or bool(
        re.search(r"профит|прибыл", text.lower())
    )


def classify(text: str | None) -> str:
    """signal | cancel | win | loss | breakeven | other (in priority order)."""
    if not text:
        return "other"
    low = text.lower()

    # 1. A fresh signal: entry + level structure, not a result/management note.
    has_entry = any(k in low for k in ("вход", "entry", "💲"))
    has_levels = any(k in low for k in ("sl", "стоп", "tp", "тейк", "target", "тп"))
    is_resultish = any(
        k in low for k in ("закры", "выход", "сработал", "перевожу", "кину", "переноси", "переста", " бу", "б/у")
    )
    if has_entry and has_levels and not is_resultish:
        return "signal"

    # 2. Cancellation / invalidation.
    if any(k in low for k in _CANCEL_KW):
        return "cancel"

    # 3. Explicit profit (a hit take-profit wins even if SL later moved to BE).
    if _LEV_PROFIT.search(text) or "тейк" in low or _has_pos_r(text) or "✅" in text:
        return "win"

    # 4. Stop to break-even (risk removed, not a loss).
    if ("бу" in low or "б/у" in low or "breakeven" in low) and "минус" not in low:
        return "breakeven"

    # 5. Loss: a hit stop or an explicit negative result.
    if _has_neg_r(text) or "loss (" in low or "стоп" in low or "по стопу" in low:
        return "loss"

    return "other"


@dataclass
class TraderScore:
    tag: str
    signals: int = 0
    wins: int = 0
    losses: int = 0
    cancels: int = 0
    breakevens: int = 0
    deleted: int = 0
    edited: int = 0
    win_leverages: list[int] = field(default_factory=list)
    loss_leverages: list[int] = field(default_factory=list)

    @property
    def resolved(self) -> int:
        return self.wins + self.losses

    @property
    def claimed_winrate(self) -> float | None:
        return round(self.wins / self.resolved * 100, 1) if self.resolved else None

    @property
    def cancel_rate(self) -> float | None:
        denom = self.signals + self.cancels
        return round(self.cancels / denom * 100, 1) if denom else None

    @property
    def avg_win_leverage(self) -> float | None:
        return round(sum(self.win_leverages) / len(self.win_leverages), 1) if self.win_leverages else None

    @property
    def avg_loss_leverage(self) -> float | None:
        return round(sum(self.loss_leverages) / len(self.loss_leverages), 1) if self.loss_leverages else None


def score_chat(session: Session, chat_id: int) -> dict[str, TraderScore]:
    messages = list(
        session.scalars(
            select(Message).where(Message.chat_id == chat_id).order_by(Message.posted_at)
        )
    )
    scores: dict[str, TraderScore] = defaultdict(lambda: TraderScore(tag="?"))
    for m in messages:
        tag = trader_tag(m.text) or "unknown"
        s = scores[tag]
        s.tag = tag
        kind = classify(m.text)
        if kind == "signal":
            s.signals += 1
        elif kind == "win":
            s.wins += 1
        elif kind == "loss":
            s.losses += 1
        elif kind == "cancel":
            s.cancels += 1
        elif kind == "breakeven":
            s.breakevens += 1
        if m.is_deleted:
            s.deleted += 1
        if m.edit_count:
            s.edited += 1
        if m.text:
            for lev in _LEV_PROFIT.findall(m.text):
                s.win_leverages.append(int(lev))
            for lev in _LEV_LOSS.findall(m.text):
                s.loss_leverages.append(int(lev))
    return dict(scores)


def render_report(scores: dict[str, TraderScore], chat_label: str = "") -> str:
    lines = [f"# Manipulation scorecard {chat_label}".rstrip(), ""]
    lines.append(
        "| trader | signals | win | loss | cancel | claimed WR | cancel% | "
        "avg lev win/loss | deleted | edited | flags |"
    )
    lines.append("|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|")
    for tag, s in sorted(scores.items(), key=lambda kv: -(kv[1].signals + kv[1].cancels)):
        if s.signals == 0 and s.resolved == 0 and s.cancels == 0:
            continue
        flags = []
        if s.avg_win_leverage and s.avg_loss_leverage and s.avg_win_leverage > s.avg_loss_leverage:
            flags.append("⚠️lev-asymmetry")
        if s.cancel_rate is not None and s.cancel_rate >= 30:
            flags.append("⚠️high-cancel")
        if s.deleted:
            flags.append(f"🗑{s.deleted}")
        lev = (
            f"{s.avg_win_leverage}/{s.avg_loss_leverage}"
            if (s.avg_win_leverage or s.avg_loss_leverage)
            else "—"
        )
        lines.append(
            f"| {tag} | {s.signals} | {s.wins} | {s.losses} | {s.cancels} | "
            f"{s.claimed_winrate if s.claimed_winrate is not None else '—'} | "
            f"{s.cancel_rate if s.cancel_rate is not None else '—'} | {lev} | "
            f"{s.deleted} | {s.edited} | {' '.join(flags) or ''} |"
        )
    lines.append("")
    lines.append(
        "_Heuristic first-pass. claimed WR is what the channel presents; cross-check "
        "with price verification (verify.py) for the REAL win-rate. 'win' counts "
        "profit posts, so re-posts of one trade inflate it (dedupe by signal_ref for "
        "unique trades). Deletion/edit columns are only meaningful once the live "
        "daemon has been running. Standalone result messages without a trader tag "
        "fall into 'unknown' (linking them needs reply context)._"
    )
    return "\n".join(lines)


def main() -> None:
    import argparse

    from ..config import load_settings
    from ..db.session import create_db_engine, make_session_factory

    parser = argparse.ArgumentParser(description="Per-trader manipulation scorecard.")
    parser.add_argument("--chat", type=int, required=True, help="chat_id to score")
    parser.add_argument("--label", default="", help="label for the report header")
    parser.add_argument("--out", default=None, help="output file (default: stdout)")
    args = parser.parse_args()

    settings = load_settings()
    engine = create_db_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    with session_factory() as session:
        scores = score_chat(session, args.chat)
    report = render_report(scores, args.label)

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(report + "\n")
        print(f"Wrote scorecard to {args.out}")
    else:
        print(report)


if __name__ == "__main__":
    main()
