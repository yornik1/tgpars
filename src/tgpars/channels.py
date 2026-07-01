"""Per-channel format profiles.

Different signal channels post in completely different formats, so the
format-specific patterns (how a signal/win/loss/leverage/trader-tag looks) must
NOT be hard-coded globally. Each channel gets a named ChannelProfile, and code
dispatches by chat_id through REGISTRY / get_profile().

What is GENERIC (works for any channel, needs no profile): capturing messages,
detecting deletions and edits. What is CHANNEL-SPECIFIC (needs a profile):
parsing signals, win/loss/leverage semantics, sub-trader tags.

To support a new channel: add a ChannelProfile with its own patterns and
register it below. Analysis tools degrade gracefully (generic-only) for channels
without a profile.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ChannelProfile:
    name: str
    chat_ids: tuple[int, ...]
    # Sub-trader tag, e.g. "[trader #c]" -> "c". None if the channel has no sub-traders.
    trader_tag: re.Pattern | None = None
    # A signal's own reference tag, e.g. "Signal ID: #c30" -> "c30".
    signal_ref: re.Pattern | None = None
    # Leverage-annotated outcome posts, e.g. "52.6% Profit (5x)" / "20% Loss (2x)".
    lev_profit: re.Pattern | None = None
    lev_loss: re.Pattern | None = None
    # Algorithmic strategy-bot posts, e.g. «Стратегия «RSI(2) Коннора» ...».
    strategy: re.Pattern | None = None
    # Sender_id of the resident "analyst" whose free-form BTC forecasts we score
    # against real price (discussion chats have no structured signals, but one
    # person posts directional calls worth grading). None if no such forecaster.
    forecaster: int | None = None
    # Notes shown to the analyst about this channel's quirks.
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def has_trader_tags(self) -> bool:
        return self.trader_tag is not None

    @property
    def has_format(self) -> bool:
        """True if we know this channel's structured-signal format. When False,
        only the generic checks (deletions, edits) are meaningful — the keyword
        classifier is not calibrated for it, so scorecard-derived facts (cancels,
        sum-R) would be noise."""
        return any((self.trader_tag, self.signal_ref, self.lev_profit, self.strategy))


# --- Channel A (multi-trader umbrella) ------------------------------------------
# Multi-trader "umbrella": sub-traders #3, #c, #d, #a, #b, #f each with their own
# style, plus algorithmic «Стратегия» bots. #3 posts 8-target ladders with a
# "(2-5x)" leverage range and reports outcomes as "N% Profit (5x)/Loss (2x)".
CHANNEL_A = ChannelProfile(
    name="channel_a",
    chat_ids=(-1001000000001,),
    trader_tag=re.compile(r"t(?:rader|arder|erder)\s*#?\s*([A-Za-z0-9]+)", re.IGNORECASE),
    signal_ref=re.compile(r"signal\s*(?:id)?[:\s]*#([A-Za-z]*\d+)", re.IGNORECASE),
    lev_profit=re.compile(r"([\d.,]+)%\s*Profit\s*\((\d+)x\)", re.IGNORECASE),
    lev_loss=re.compile(r"([\d.,]+)%\s*Loss\s*\((\d+)x\)", re.IGNORECASE),
    strategy=re.compile(r"Стратегия\s*«([^»]+)»"),
    notes=(
        "Sub-trader #3 = umbrella-channel style: 8-target ladders, '(2-5x)', "
        "reports 'N% Profit (5x)' / 'N% Loss (2x)'.",
        "#a/#c abuse 'stop to break-even' to turn losses into flat outcomes.",
        "#b posts spot trades and never reports R (uses 'потенциальная прибыль').",
        "#f runs a forex/XAU desk with weekly aggregate stats.",
        "Algorithmic «Стратегия» bots are a separate stream (virtual/test trades).",
    ),
)

# --- Channel B (discussion chat) -------------------------------------------------------
# Mostly a discussion chat, not a structured-signal channel; no reliable format.
CHANNEL_B = ChannelProfile(
    name="channel_b",
    chat_ids=(-1001000000002,),
    forecaster=1000000001,  # resident BTC forecaster (sender id)
    notes=(
        "Discussion chat, not a structured-signal channel — generic checks only.",
        "A resident analyst posts free-form BTC forecasts (no "
        "entry/stop/target); use --scorecard to grade their short-term direction "
        "against real price.",
    ),
)


REGISTRY: dict[int, ChannelProfile] = {
    cid: profile for profile in (CHANNEL_A, CHANNEL_B) for cid in profile.chat_ids
}

# Fallback for channels we have not profiled yet: no format patterns, so only the
# generic checks (deletions, edits) apply.
GENERIC = ChannelProfile(name="generic", chat_ids=())


def get_profile(chat_id: int) -> ChannelProfile:
    return REGISTRY.get(chat_id, GENERIC)
