"""Planner agent: finds candidate trains per leg. Tools: travel_search.search_trains."""

from __future__ import annotations

from fastmcp.exceptions import ToolError

from regulated_agent_lab.agents.runtime import NodeContext
from regulated_agent_lab.agents.state import LabState, TripRequest

AGENT = "planner"
SEARCH = "travel_search.search_trains"

# Stations served by the (fictional) Demo Rail network, with accepted aliases.
KNOWN_STATIONS: tuple[str, ...] = ("Zurich HB", "Bern", "Geneva", "Basel")
_ALIASES: dict[str, str] = {
    "zurich": "Zurich HB",
    "zürich": "Zurich HB",
    "zurich hb": "Zurich HB",
    "zürich hb": "Zurich HB",
    "bern": "Bern",
    "berne": "Bern",
    "geneva": "Geneva",
    "genève": "Geneva",
    "geneve": "Geneva",
    "basel": "Basel",
    "bâle": "Basel",
}


def normalize_station(name: str) -> str | None:
    """Canonical station name, or None if Demo Rail does not serve it."""
    return _ALIASES.get(name.strip().lower())


def legs(trip: TripRequest) -> list[tuple[str, str, str, str]]:
    """(leg, origin, destination, ISO date) for each leg of the trip."""
    out = [("outbound", trip.origin, trip.destination, trip.depart_date.isoformat())]
    if trip.return_date is not None:
        out.append(("return", trip.destination, trip.origin, trip.return_date.isoformat()))
    return out


async def run(state: LabState, ctx: NodeContext) -> dict:
    intent = state["intent"]
    assert intent is not None and intent.trip is not None
    candidates: dict[str, list[dict]] = {}
    windows = {"outbound": intent.trip.outbound_window, "return": intent.trip.return_window}
    for leg, origin, destination, day in legs(intent.trip):
        args = {"origin": origin, "destination": destination, "date": day}
        window = windows[leg]
        if window is not None and window.earliest is not None:
            # Start the timetable page at the window: a live source returns a bounded page.
            args["after"] = window.earliest.strftime("%H:%M")
        try:
            guard = await ctx.call(SEARCH, args)
        except ToolError:
            ctx.record(
                AGENT, "needs_clarification", {"reason": "timetable unavailable", "leg": leg}
            )
            return {"status": "needs_clarification"}
        trains = guard.sanitized or []
        if not trains:
            ctx.record(AGENT, "needs_clarification", {"reason": "no direct train", "leg": leg})
            return {"status": "needs_clarification"}
        candidates[leg] = trains
    return {"candidates": candidates}
