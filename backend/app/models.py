from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class Stage(StrEnum):
    FOUND = "found"
    QUALIFIED = "qualified"
    DISQUALIFIED = "disqualified"
    SAMPLE_BUILT = "sample_built"
    AWAITING_APPROVAL = "awaiting_approval"
    PITCHED = "pitched"  # sent, waiting for the lead
    NEGOTIATING = "negotiating"  # lead replied; our answer is drafted
    WON = "won"
    DELIVERED = "delivered"  # full research sent after the win
    LOST = "lost"
    REJECTED = "rejected"


class Signal(BaseModel):
    """One piece of public evidence that someone needs data."""

    source: str  # jobs | reddit | news
    title: str
    url: str | None = None
    snippet: str = ""
    posted: str | None = None
    company: str | None = None
    location: str | None = None
    market: str | None = None  # which market's search found it (None = global, e.g. Reddit)


class Lead(BaseModel):
    id: str
    name: str
    source: str
    location: str | None = None
    market: str | None = None
    currency: str = "USD"
    email: str | None = None
    intent_score: int
    evidence: list[str]
    signals: list[Signal]


# --- LLM structured outputs -------------------------------------------------


class Assessment(BaseModel):
    fit_score: int = Field(description="0-100: how likely this lead buys our research service")
    need_summary: str = Field(description="One sentence: what data they need and why")
    sample_category: str = Field(description="Business type to sample, e.g. 'real estate agencies'")
    sample_city: str = Field(description="City to sample, in the lead's own country, e.g. 'Pune' or 'Austin'")
    angle: str = Field(description="The hook for the pitch, grounded in the evidence")


class Pitch(BaseModel):
    subject: str
    body: str


ReplyIntent = Literal["interested", "question", "price_objection", "accept", "not_now", "unsubscribe"]


class ReplyRead(BaseModel):
    intent: ReplyIntent = Field(
        description="interested: wants to proceed or know more; question: asks something specific; "
        "price_objection: pushes back on price or asks for a discount; accept: agrees to buy at the "
        "offered price; not_now: declines for now; unsubscribe: asks not to be emailed"
    )
    requested_rows: int | None = Field(description="How many rows/records they asked for, if they said")
    summary: str = Field(description="One line: what they said")


class EmailDraft(BaseModel):
    body: str
