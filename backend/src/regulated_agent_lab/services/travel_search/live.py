"""Live timetable source: real direct connections from a public timetable API.

Opt-in with ``TRAVEL_SEARCH_SOURCE=live``. Only the schedule is real; fares and seat holds stay
simulated (public timetable APIs publish no fares and sell nothing), priced by station pair
from ``data.PRICES_CENTS`` with the same off-peak saver rule (by departure hour). Stations are
queried by numeric id, so no operator name lives in this code; whatever the API returns at
runtime is untrusted tool output and goes through the injection guard like any other result.
"""

from __future__ import annotations

import os
import re
from datetime import date, datetime
from typing import Any

import httpx

from regulated_agent_lab.services.travel_search import data

DEFAULT_API_URL = "https://transport.opendata.ch/v1"
API_URL_ENV = "TIMETABLE_API_URL"
# Below the MCP call deadline (MCP_CALL_TIMEOUT_S, default 10 s), so a slow API is reported by
# this server as "timetable unavailable" instead of surfacing as a stalled tool call.
REQUEST_TIMEOUT_S = 8.0
FIRST_QUERY_TIME = "06:00"
MAX_CONNECTIONS = 16  # the API's own upper bound per request
LIVE_PREFIX = "LV"

# Canonical lab station name -> public station id.
STATION_IDS: dict[str, str] = {
    "Zurich HB": "8503000",
    "Bern": "8507000",
    "Geneva": "8501008",
    "Basel": "8500010",
}

_PRODUCT = re.compile(r"[^A-Z0-9]")
_FIELDS = (
    "connections/from/departure",
    "connections/to/arrival",
    "connections/transfers",
    "connections/products",
)


class TimetableUnavailable(RuntimeError):
    """The public API did not answer usefully (network, HTTP status or payload shape)."""


def _local(stamp: str) -> datetime:
    """``2026-12-01T07:02:00+0100`` -> naive local wall-clock time (the lab's convention)."""
    return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S%z").replace(tzinfo=None)


def make_train_id(origin: str, destination: str, depart: datetime, product: str) -> str:
    code = _PRODUCT.sub("", product.upper())[:8] or "X"
    return (
        f"{LIVE_PREFIX}-{data.STATIONS[origin]}-{data.STATIONS[destination]}-"
        f"{depart:%Y%m%d-%H%M}-{code}"
    )


_CLOCK = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def valid_clock(value: str) -> bool:
    """``HH:MM`` on a 24-hour clock."""
    return bool(_CLOCK.match(value))


def _fetch(
    origin: str, destination: str, day: date, after: str | None = None, transport: Any = None
) -> list[dict]:
    params: list[tuple[str, str]] = [
        ("from", STATION_IDS[origin]),
        ("to", STATION_IDS[destination]),
        ("date", day.isoformat()),
        # The API returns one bounded page of trains from this time, so an evening window must
        # start the page in the evening rather than at the first train of the day.
        ("time", after if after and valid_clock(after) else FIRST_QUERY_TIME),
        ("limit", str(MAX_CONNECTIONS)),
        *(("fields[]", f) for f in _FIELDS),
    ]
    url = os.environ.get(API_URL_ENV, DEFAULT_API_URL).rstrip("/") + "/connections"
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_S, transport=transport) as client:
            response = client.get(url, params=params)
        response.raise_for_status()
        connections = response.json()["connections"]
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise TimetableUnavailable(type(exc).__name__) from exc
    if not isinstance(connections, list):
        raise TimetableUnavailable("connections is not a list")
    return connections


def search_trains(
    origin: str,
    destination: str,
    day: date,
    poisoned: bool = False,
    after: str | None = None,
    transport: Any = None,
) -> list[dict]:
    """Direct trains on ``day`` in the lab's train shape, earliest first."""
    if origin not in STATION_IDS or destination not in STATION_IDS:
        raise data.UnknownStation(origin if origin not in STATION_IDS else destination)
    if origin == destination:
        return []
    trains: list[dict] = []
    seen: set[str] = set()
    for conn in _fetch(origin, destination, day, after, transport):
        if not isinstance(conn, dict):
            continue
        try:
            if conn.get("transfers") != 0:
                continue  # the lab books one train per leg
            depart = _local(conn["from"]["departure"])
            arrive = _local(conn["to"]["arrival"])
            product = (conn.get("products") or ["X"])[0]
        except (KeyError, TypeError, ValueError):
            continue  # skip malformed rows rather than trusting them
        if depart.date() != day:
            continue
        train_id = make_train_id(origin, destination, depart, str(product))
        if train_id in seen:
            continue
        seen.add(train_id)
        trains.append(
            {
                "train_id": train_id,
                "origin": origin,
                "destination": destination,
                "depart": depart.isoformat(),
                "arrive": arrive.isoformat(),
                "operator": "live timetable",
                "product": str(product),
                "notes": "",
            }
        )
    trains.sort(key=lambda t: t["depart"])
    if poisoned and trains:
        trains[0]["notes"] = data.POISONED_NOTE
    return trains
