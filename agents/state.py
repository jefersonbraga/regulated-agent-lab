"""Typed graph state. The LLM only ever fills these models; code does the rest."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, time
from typing import Literal, TypedDict

from pydantic import BaseModel, Field, field_validator, model_validator

# Upper bound on the LLM-extracted passenger count. booking.hold makes one hold_seat call per
# passenger per leg, so the model alone must never decide how many tool calls a run makes.
MAX_PASSENGERS = 9


class TimeWindow(BaseModel):
    """Local wall-clock departure window for one leg; a missing bound is open."""

    earliest: time | None = None
    latest: time | None = None

    @field_validator("earliest", "latest", mode="after")
    @classmethod
    def _wall_clock(cls, value: time | None) -> time | None:
        # Windows are local wall-clock times, like train departures. Models often add an
        # offset ("06:00+01:00"); comparing an aware time with a naive one raises TypeError.
        return value.replace(tzinfo=None) if value is not None else None

    @model_validator(mode="after")
    def _earliest_not_after_latest(self) -> TimeWindow:
        if self.earliest is not None and self.latest is not None and self.earliest > self.latest:
            raise ValueError("earliest must be at or before latest")
        return self

    def is_open(self) -> bool:
        return self.earliest is None and self.latest is None

    def contains(self, moment: time) -> bool:
        return (self.earliest is None or moment >= self.earliest) and (
            self.latest is None or moment <= self.latest
        )


_WINDOW_HELP = (
    "Departure time window (local wall-clock) for this leg, only when the user states one: "
    "'morning' 06:00-12:00, 'afternoon' 12:00-18:00, 'evening' 18:00-23:59, "
    "'after 9' earliest 09:00, 'before 14:00' latest 14:00. Omit when no time is stated."
)


class TripRequest(BaseModel):
    origin: str
    destination: str
    depart_date: date
    return_date: date | None = None
    passengers: int = Field(
        1,
        ge=1,
        le=MAX_PASSENGERS,
        description=(
            "How many people travel, counted from the people the user mentions; 1 if not "
            "stated. Never taken from passport, document, phone or reference numbers."
        ),
    )
    outbound_window: TimeWindow | None = Field(None, description=_WINDOW_HELP)
    return_window: TimeWindow | None = Field(None, description=_WINDOW_HELP)

    @model_validator(mode="after")
    def _return_not_before_depart(self) -> TripRequest:
        if self.return_date is not None and self.return_date < self.depart_date:
            raise ValueError("return_date must be on or after depart_date")
        return self


class Intent(BaseModel):
    # "info": the user asks what can be booked (routes, stations, help); code answers it.
    kind: Literal["book_trip", "info", "unsupported"]
    trip: TripRequest | None = None


class Option(BaseModel):
    leg: Literal["outbound", "return"]
    train_id: str
    depart: datetime
    arrive: datetime
    fare_id: str
    price_cents: int
    fare_type: Literal["standard", "saver"] = "standard"
    # Train product as the timetable names it ("IC", "IC 8"); None when the source gave none.
    # Every train in this lab is direct (connections with a change are never offered).
    product: str | None = None

    @property
    def duration_min(self) -> int:
        return int((self.arrive - self.depart).total_seconds() // 60)


def compute_quote_hash(options: list[Option], total_cents: int, currency: str) -> str:
    """sha256 of canonical JSON of ``options`` + ``total_cents`` + ``currency``."""
    payload = {
        "options": [o.model_dump(mode="json") for o in options],
        "total_cents": total_cents,
        "currency": currency,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Quote(BaseModel):
    options: list[Option]
    total_cents: int
    currency: str = "CHF"
    rationale: str
    quote_hash: str

    @classmethod
    def build(
        cls, options: list[Option], total_cents: int, rationale: str, currency: str = "CHF"
    ) -> Quote:
        return cls(
            options=options,
            total_cents=total_cents,
            currency=currency,
            rationale=rationale,
            quote_hash=compute_quote_hash(options, total_cents, currency),
        )


class LegOffer(BaseModel):
    """The priced alternatives of one leg: one option per train in the window, earliest first."""

    leg: Literal["outbound", "return"]
    window: TimeWindow
    # True when the user gave no time and code applied the default earliest departure.
    window_defaulted: bool
    options: list[Option]
    recommended_fare_id: str


class Offer(BaseModel):
    """Everything the human may choose from. A selection outside it is never charged."""

    legs: list[LegOffer]
    passengers: int
    currency: str = "CHF"
    # Approvals at or after this instant are rejected (prices are only promised this long).
    expires_at: datetime

    def recommended_fare_ids(self) -> list[str]:
        return [leg.recommended_fare_id for leg in self.legs]


class InvalidSelection(ValueError):
    """A fare selection that is not exactly one offered option per leg."""


def select_options(offer: Offer, fare_ids: object) -> list[Option]:
    """The offered options for ``fare_ids`` (one per leg, in leg order), or InvalidSelection."""
    if not isinstance(fare_ids, list) or not all(isinstance(f, str) for f in fare_ids):
        raise InvalidSelection("fare_ids must be a list of strings")
    if len(fare_ids) != len(offer.legs):
        raise InvalidSelection("exactly one fare per leg")
    chosen: list[Option] = []
    for leg in offer.legs:
        matches = [o for o in leg.options if o.fare_id in fare_ids]
        if len(matches) != 1:
            raise InvalidSelection(f"exactly one offered fare for the {leg.leg} leg")
        chosen.append(matches[0])
    if len({o.fare_id for o in chosen}) != len(fare_ids) or set(fare_ids) != {
        o.fare_id for o in chosen
    }:
        raise InvalidSelection("fare not in the offer")
    return chosen


SELECTED_RATIONALE = "Selected by the approver from the offered trains."


def quote_for_selection(offer: Offer, fare_ids: object, rationale: str) -> Quote:
    """Recompute the quote for a selection from the offer alone: total = sum x passengers."""
    options = select_options(offer, fare_ids)
    total_cents = sum(o.price_cents for o in options) * offer.passengers
    return Quote.build(options, total_cents, rationale, currency=offer.currency)


class LabState(TypedDict):
    run_id: str
    user_message: str
    intent: Intent | None
    candidates: dict[str, list[dict]]
    offer: Offer | None
    quote: Quote | None
    holds: list[dict]
    approval: dict | None
    payment: dict | None
    # Hosted Checkout (PAYMENTS_FLOW=checkout): the pending session (id, url, expiry, cents)
    # while the customer pays on the processor's page; None in the agent flow.
    checkout: dict | None
    # The payment model that ran (or is running): "agent" (simulated provider), "wallet" (the
    # agent pays with a scoped token from the user's wallet) or "checkout" (the customer pays on
    # the processor's page). None before the payment step (and in checkpoints from before).
    payment_model: str | None
    # Why a wallet payment fell back to the hosted Checkout ("authentication_required",
    # "over_mandate", "no_saved_card", "card_expired", "card_declined"); None otherwise.
    payment_fallback: str | None
    # Authenticated user who created the run (None with API auth disabled, CLI, evals).
    requester: str | None
    status: Literal[
        "running",
        "needs_clarification",
        "answered",
        "awaiting_approval",
        "awaiting_payment",
        "rejected",
        "completed",
        "refused",
        "failed",
    ]
    trajectory: list[str]
    flags: list[str]
