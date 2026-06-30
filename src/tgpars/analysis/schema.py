"""Flexible parsed-signal schema.

Signal channels use wildly different formats (forex vs crypto, 1..N targets,
optional stop, market vs limit, terse vs verbose, plus pure chatter and result
updates). So the schema is intentionally loose: a message-type discriminator
with mostly-optional fields, and a free-text ``raw_notes`` so nothing that does
not fit is silently lost.

The ``signal_ref`` / ``trader_tag`` fields are the key to linking a later result
update back to the original signal (channels tag them, e.g. ``Signal: #f10``).
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class MessageType(str, Enum):
    SIGNAL = "signal"  # a new trade call
    UPDATE = "update"  # progress/result on an existing signal (TP hit, BE, closed)
    CANCEL = "cancel"  # signal cancelled / invalidated before playing out
    CHATTER = "chatter"  # discussion, opinions, off-topic
    OTHER = "other"  # liquidation feeds, links, anything else


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"


class AssetClass(str, Enum):
    CRYPTO = "crypto"
    FOREX = "forex"
    OTHER = "other"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"


class Outcome(str, Enum):
    TP_HIT = "tp_hit"  # all targets hit
    PARTIAL_TP = "partial_tp"  # some targets hit
    SL_HIT = "sl_hit"
    BREAKEVEN = "breakeven"
    CLOSED_MANUAL = "closed_manual"  # closed by hand, neither full TP nor SL
    CANCELLED = "cancelled"


class SignalDetails(BaseModel):
    asset: str | None = Field(None, description="e.g. XAU/USD, BTCUSDT, SKY/USDT")
    asset_class: AssetClass | None = None
    direction: Direction | None = Field(None, description="normalize buy->long, sell->short")
    order_type: OrderType | None = None
    entry_low: float | None = None
    entry_high: float | None = Field(None, description="equals entry_low for a single price")
    stop_loss: float | None = None
    targets: list[float] = Field(default_factory=list, description="0..N, ordered")
    leverage: str | None = Field(None, description="verbatim, e.g. '2-5x'")
    risk_pct: float | None = None
    raw_notes: str | None = Field(None, description="anything that did not fit the fields")


class UpdateDetails(BaseModel):
    refers_to_ref: str | None = Field(None, description="signal tag this update is about")
    outcome: Outcome | None = None
    targets_hit: int | None = None
    claimed_pnl_pct: float | None = Field(None, description="profit/loss the author claims")
    moved_sl_to_be: bool | None = None


class ParsedMessage(BaseModel):
    message_type: MessageType
    trader_tag: str | None = Field(None, description="sub-trader id within a channel, e.g. f, c, A")
    signal_ref: str | None = Field(None, description="this message's own tag, e.g. #f10")
    confidence: float = Field(..., ge=0.0, le=1.0)
    signal: SignalDetails | None = None
    update: UpdateDetails | None = None
