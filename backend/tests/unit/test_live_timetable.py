"""Live timetable adapter, against a recorded-shape fake API (never the network)."""

from datetime import date

import httpx
import pytest

from regulated_agent_lab.services.travel_search import data, live

DAY = date(2026, 12, 1)


def _conn(depart: str, arrive: str, transfers: int = 0, product: str = "IC 8") -> dict:
    return {
        "from": {"departure": f"2026-12-01T{depart}:00+0100"},
        "to": {"arrival": f"2026-12-01T{arrive}:00+0100"},
        "transfers": transfers,
        "products": [product],
    }


def _api(connections, status: int = 200, seen: list | None = None) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json={"connections": connections})

    return httpx.MockTransport(handler)


def test_maps_direct_connections_to_lab_trains():
    seen: list[httpx.Request] = []
    api = _api(
        [
            _conn("07:32", "08:28", product="IC 1"),
            _conn("06:02", "06:58"),
            _conn("06:08", "07:24", transfers=1),  # needs a change: not bookable here
        ],
        seen=seen,
    )
    trains = live.search_trains("Zurich HB", "Bern", DAY, transport=api)

    assert [t["train_id"] for t in trains] == [
        "LV-ZUE-BER-20261201-0602-IC8",
        "LV-ZUE-BER-20261201-0732-IC1",
    ]
    assert trains[0] == {
        "train_id": "LV-ZUE-BER-20261201-0602-IC8",
        "origin": "Zurich HB",
        "destination": "Bern",
        "depart": "2026-12-01T06:02:00",
        "arrive": "2026-12-01T06:58:00",
        "operator": "live timetable",
        "product": "IC 8",
        "notes": "",
    }
    # Stations are queried by id, for the requested date.
    params = seen[0].url.params
    assert params["from"] == "8503000" and params["to"] == "8507000"
    assert params["date"] == "2026-12-01"


def test_live_trains_get_simulated_fares_and_holds():
    (train,) = live.search_trains(
        "Zurich HB", "Bern", DAY, transport=_api([_conn("07:02", "07:58")])
    )
    fares = data.get_fares(train["train_id"])
    assert [(f["class_"], f["price_cents"]) for f in fares] == [("2nd", 5600), ("1st", 9800)]
    hold = data.hold_seat(fares[0]["fare_id"], "pax-1")
    assert hold["price_cents"] == 5600


@pytest.mark.parametrize(
    "train_id",
    [
        "LV-ZUE-ZUE-20261201-0702-IC8",  # same station
        "LV-ZUE-BER-20261201-0702",  # live id without product
        "LV-ZUE-BER-20261201-0702-IC-8",  # extra part
        "DR-ZUE-BER-20261201-0732",  # simulated ids keep their fixed :02 departures
        "XX-ZUE-BER-20261201-0702-IC8",
    ],
)
def test_malformed_train_ids_are_unknown(train_id):
    with pytest.raises(data.UnknownItem):
        data.get_fares(train_id)


def test_malformed_rows_are_skipped_not_trusted():
    rows = [{"from": {}, "to": {}, "transfers": 0}, "junk", _conn("09:02", "09:58")]
    trains = live.search_trains("Zurich HB", "Bern", DAY, transport=_api(rows))
    assert [t["depart"] for t in trains] == ["2026-12-01T09:02:00"]


@pytest.mark.parametrize("status", [404, 500, 503])
def test_api_errors_are_timetable_unavailable(status):
    with pytest.raises(live.TimetableUnavailable):
        live.search_trains("Zurich HB", "Bern", DAY, transport=_api([], status=status))


def test_network_error_is_timetable_unavailable():
    def down(request):
        raise httpx.ConnectError("unreachable")

    with pytest.raises(live.TimetableUnavailable):
        live.search_trains("Zurich HB", "Bern", DAY, transport=httpx.MockTransport(down))


def test_unknown_station_is_rejected_before_any_request():
    def never(request):
        raise AssertionError("no request expected")

    with pytest.raises(data.UnknownStation):
        live.search_trains("Atlantis", "Bern", DAY, transport=httpx.MockTransport(never))


def test_poisoned_mode_still_applies_to_live_results():
    rows = [_conn("07:02", "07:58")]
    (train,) = live.search_trains("Zurich HB", "Bern", DAY, poisoned=True, transport=_api(rows))
    assert train["notes"] == data.POISONED_NOTE


def test_window_start_moves_the_timetable_page():
    # One page of trains from 06:00 never reaches an evening window: query from the window.
    seen: list[httpx.Request] = []
    api = _api([_conn("18:02", "18:58")], seen=seen)
    live.search_trains("Zurich HB", "Bern", DAY, after="18:00", transport=api)
    live.search_trains("Zurich HB", "Bern", DAY, transport=api)
    assert [r.url.params["time"] for r in seen] == ["18:00", live.FIRST_QUERY_TIME]


@pytest.mark.parametrize("value", ["18:00", "00:00", "23:59"])
def test_valid_clock_accepts_24h_times(value):
    assert live.valid_clock(value)


@pytest.mark.parametrize("value", ["24:00", "7:00", "18:60", "18:00:00", "evening", ""])
def test_valid_clock_rejects_anything_else(value):
    assert not live.valid_clock(value)


async def test_search_tool_rejects_a_malformed_time():
    from fastmcp import Client
    from fastmcp.exceptions import ToolError

    from regulated_agent_lab.services.travel_search.server import mcp

    async with Client(mcp) as client:
        with pytest.raises(ToolError, match="invalid time"):
            await client.call_tool(
                "search_trains",
                {
                    "origin": "Zurich HB",
                    "destination": "Bern",
                    "date": "2026-12-01",
                    "after": "25:00",
                },
            )
