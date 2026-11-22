"""travel_search MCP server: Demo Rail schedule, fares and seat holds.

The schedule is simulated by default; ``TRAVEL_SEARCH_SOURCE=live`` reads real direct trains
from a public timetable API instead (see ``live.py``). Fares and holds are always simulated.

Run over stdio with: python -m regulated_agent_lab.services.travel_search.server
"""

from __future__ import annotations

import os
from datetime import date as date_type

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError

from regulated_agent_lab.services.travel_search import data, live

mcp = FastMCP("travel_search")


def _poisoned() -> bool:
    return os.environ.get("TRAVEL_SEARCH_POISONED") == "1"


def _live() -> bool:
    return os.environ.get("TRAVEL_SEARCH_SOURCE", "simulated") == "live"


@mcp.tool
def search_trains(origin: str, destination: str, date: str, after: str | None = None) -> list[dict]:
    """List direct trains between two stations on a date (YYYY-MM-DD).

    ``after`` (HH:MM, optional) is where the timetable starts looking; the live source returns a
    bounded page of trains from that time, the simulated schedule always lists the whole day.
    """
    try:
        day = date_type.fromisoformat(date)
    except ValueError as exc:
        raise ToolError("invalid date") from exc
    if after is not None and not live.valid_clock(after):
        raise ToolError("invalid time")
    try:
        if _live():
            return live.search_trains(origin, destination, day, poisoned=_poisoned(), after=after)
        return data.search_trains(origin, destination, day, poisoned=_poisoned())
    except data.UnknownStation as exc:
        raise ToolError("unknown station") from exc
    except live.TimetableUnavailable as exc:
        raise ToolError("timetable unavailable") from exc


@mcp.tool
def get_fares(train_id: str) -> list[dict]:
    """List 2nd and 1st class fares, plus an off-peak 2nd-class saver (integer CHF cents)."""
    try:
        return data.get_fares(train_id)
    except data.UnknownItem as exc:
        raise ToolError("unknown train") from exc


@mcp.tool
def hold_seat(fare_id: str, passenger_ref: str) -> dict:
    """Place a temporary seat hold on a fare for a (pseudonymous) passenger reference."""
    try:
        return data.hold_seat(fare_id, passenger_ref)
    except data.UnknownItem as exc:
        raise ToolError("unknown fare") from exc


if __name__ == "__main__":
    mcp.run(show_banner=False)
