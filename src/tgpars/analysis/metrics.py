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
# Algorithmic strategy-bot posts (a separate stream from the human sub-traders),
# e.g. «Стратегия «RSI(2) Коннора» закрыла ЛОНГ ... Результат: +3.1R».
_STRAT_RE = re.compile(r"Стратегия\s*«([^»]+)»")
_RESULT_R = re.compile(r"Результат:\s*([+\-−]?\d+(?:[.,]\d+)?)\s*R")
# Any realized-R figure in a result post, e.g. "+1.27R", "−0.74R", "1.01R".
_ANY_R = re.compile(r"([+\-−]?\d+(?:[.,]\d+)?)\s*R\b")
# This message's own signal reference, e.g. "#2170", "#c19", "#f9".
_REF_RE = re.compile(r"signal\s*(?:id)?[:\s]*#([A-Za-z]*\d+)", re.IGNORECASE)

_CANCEL_KW = ("отмен", "инвалид", "не актуально", "без нас", "не дотян", "ушло без", "ушла без")
_LOSS_KW = ("стоп", "loss", "минус", "по стопу", "сработал стоп")
_WIN_KW = ("тейк", "profit", "прибыл", "закрыли оба", "закрыла +", "закрыл +", "✅")


def trader_tag(text: str | None) -> str | None:
    """Best-effort sub-trader id from a message."""
    if not text:
        return None
    m = _STRAT_RE.search(text)
    if m:  # algorithmic strategy bot -> its own bucket, not a human trader
        return f"strat:{m.group(1)}"
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


def _to_float(raw: str) -> float | None:
    try:
        return float(raw.replace("−", "-").replace(",", "."))
    except ValueError:
        return None


def _parse_result_r(text: str) -> float | None:
    """Parse a strategy bot's 'Результат: ±N.NR' value (handles unicode minus)."""
    m = _RESULT_R.search(text)
    return _to_float(m.group(1)) if m else None


def realized_r(text: str | None) -> float | None:
    """Realized R from any result post (strategy 'Результат:' or human '+1.27R')."""
    if not text:
        return None
    r = _parse_result_r(text)
    if r is not None:
        return r
    matches = _ANY_R.findall(text)
    return _to_float(matches[-1]) if matches else None  # last R figure = the result


def signal_ref(text: str | None) -> str | None:
    """This message's signal reference tag, e.g. '#2170', '#c19', '#f9'."""
    if not text:
        return None
    m = _REF_RE.search(text)
    return f"#{m.group(1).lower()}" if m else None


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

    # 0. Strategy-bot posts are self-contained: open -> signal, close -> win/loss
    #    by the stated ±R (covers ✅ target, 🛑 stop, ⚪ reverse-signal exit).
    if "стратегия" in low and "«" in text:
        if "открыла" in low:
            return "signal"
        if "закрыла" in low:
            r = _parse_result_r(text)
            if "поймала стоп" in low or (r is not None and r < 0):
                return "loss"
            if "цель достигнута" in low or (r is not None and r > 0):
                return "win"
            return "other"

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
    sum_r: float = 0.0
    r_count: int = 0
    win_refs: set[str] = field(default_factory=set)

    @property
    def resolved(self) -> int:
        return self.wins + self.losses

    @property
    def total_r(self) -> float | None:
        return round(self.sum_r, 2) if self.r_count else None

    @property
    def unique_wins(self) -> int | None:
        """Distinct winning signals (dedupes re-posts of one trade)."""
        return len(self.win_refs) if self.win_refs else None

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


def _resolve_tags(messages: list[Message]) -> dict[int, str | None]:
    """Map each message_id to a trader tag, inheriting via the reply_to chain.

    A result/update post usually carries no tag but is a reply to the original
    (tagged) signal, so we walk reply_to up to its tagged ancestor.
    """
    by_id = {m.message_id: m for m in messages}
    direct = {m.message_id: trader_tag(m.text) for m in messages}
    resolved: dict[int, str | None] = {}

    def resolve(mid: int) -> str | None:
        seen: set[int] = set()
        cur: int | None = mid
        while cur is not None and cur not in seen:
            seen.add(cur)
            if direct.get(cur):
                return direct[cur]
            parent = by_id.get(cur)
            cur = parent.reply_to_msg_id if parent else None
        return None

    for m in messages:
        resolved[m.message_id] = resolve(m.message_id)
    return resolved


def score_chat(session: Session, chat_id: int) -> dict[str, TraderScore]:
    messages = list(
        session.scalars(
            select(Message).where(Message.chat_id == chat_id).order_by(Message.posted_at)
        )
    )
    tags = _resolve_tags(messages)
    scores: dict[str, TraderScore] = defaultdict(lambda: TraderScore(tag="?"))
    for m in messages:
        tag = tags.get(m.message_id) or "unknown"
        s = scores[tag]
        s.tag = tag
        kind = classify(m.text)
        if kind == "signal":
            s.signals += 1
        elif kind == "win":
            s.wins += 1
            ref = signal_ref(m.text)
            if ref:
                s.win_refs.add(ref)
        elif kind == "loss":
            s.losses += 1
        elif kind == "cancel":
            s.cancels += 1
        elif kind == "breakeven":
            s.breakevens += 1
        if kind in ("win", "loss"):
            r = realized_r(m.text)
            if r is not None:
                s.sum_r += r
                s.r_count += 1
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
        "| trader | signals | win | uniq win | loss | cancel | claimed WR | "
        "sum R | cancel% | avg lev win/loss | deleted | edited | flags |"
    )
    lines.append("|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|")
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
        if s.total_r is not None and s.total_r < 0:
            flags.append("⚠️negative-R")
        lines.append(
            f"| {tag} | {s.signals} | {s.wins} | "
            f"{s.unique_wins if s.unique_wins is not None else '—'} | "
            f"{s.losses} | {s.cancels} | "
            f"{s.claimed_winrate if s.claimed_winrate is not None else '—'} | "
            f"{s.total_r if s.total_r is not None else '—'} | "
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
