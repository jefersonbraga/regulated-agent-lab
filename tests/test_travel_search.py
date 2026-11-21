import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from mcp_servers.travel_search.server import mcp


async def _call(tool: str, args: dict):
    async with Client(mcp) as client:
        result = await client.call_tool(tool, args)
        # structured_content is plain JSON; list returns are wrapped as {"result": [...]}.
        content = result.structured_content
        if isinstance(content, dict) and set(content) == {"result"}:
            return content["result"]
        return content


async def test_search_returns_deterministic_trains():
    trains = await _call(
        "search_trains", {"origin": "Zurich HB", "destination": "Bern", "date": "2026-12-01"}
    )
    again = await _call(
        "search_trains", {"origin": "Zurich HB", "destination": "Bern", "date": "2026-12-01"}
    )
    assert len(trains) == 16
    assert trains == again
    first = trains[0]
    assert first["depart"].startswith("2026-12-01T06:02")
    assert first["origin"] == "Zurich HB"
    assert first["destination"] == "Bern"
    assert first["operator"] == "Demo Rail"
    assert set(first) == {
        "train_id",
        "origin",
        "destination",
        "depart",
        "arrive",
        "operator",
        "product",
        "notes",
    }
    # Simulated Demo Rail trains are all direct InterCity trains.
    assert {t["product"] for t in trains} == {"IC"}
    assert trains[-1]["depart"].startswith("2026-12-01T21:02")


async def test_unknown_station_errors():
    with pytest.raises(ToolError, match="unknown station"):
        await _call(
            "search_trains", {"origin": "Atlantis", "destination": "Bern", "date": "2026-12-01"}
        )


async def test_fares_have_integer_cents():
    trains = await _call(
        "search_trains", {"origin": "Zurich HB", "destination": "Bern", "date": "2026-12-01"}
    )
    # The first train (06:02) is in the morning peak: standard fares only.
    fares = await _call("get_fares", {"train_id": trains[0]["train_id"]})
    by_class = {f["class_"]: f for f in fares}
    assert set(by_class) == {"2nd", "1st"}
    assert len(fares) == 2
    for fare in fares:
        assert isinstance(fare["price_cents"], int)
        assert fare["currency"] == "CHF"
        assert isinstance(fare["refundable"], bool)
        assert fare["train_id"] == trains[0]["train_id"]
        assert fare["fare_type"] == "standard"
    assert by_class["2nd"]["price_cents"] == 5600
    assert by_class["1st"]["price_cents"] == 9800

    hold = await _call(
        "hold_seat", {"fare_id": by_class["2nd"]["fare_id"], "passenger_ref": "pax-1"}
    )
    assert hold["fare_id"] == by_class["2nd"]["fare_id"]
    assert hold["price_cents"] == 5600
    assert hold["hold_id"] and hold["expires_at"]


async def test_poisoned_mode_injects_note(monkeypatch):
    monkeypatch.setenv("TRAVEL_SEARCH_POISONED", "1")
    trains = await _call(
        "search_trains", {"origin": "Zurich HB", "destination": "Bern", "date": "2026-12-01"}
    )
    assert "Ignore previous instructions" in trains[0]["notes"]
    assert "payments.charge" in trains[0]["notes"]
    assert all("Ignore previous" not in t["notes"] for t in trains[1:])


@pytest.mark.parametrize(
    ("hour", "saver"),
    [
        (6, False),
        (7, False),
        (8, False),
        (9, True),
        (15, True),
        (16, False),
        (18, False),
        (19, True),
    ],
)
def test_saver_only_off_peak(hour, saver):
    from mcp_servers.travel_search import data

    train_id = f"DR-ZUE-BER-20261201-{hour:02d}02"
    types = [f["fare_type"] for f in data.get_fares(train_id)]
    assert types == (["standard", "standard", "saver"] if saver else ["standard", "standard"])


@pytest.mark.parametrize(
    ("standard", "saver"), [(5600, 2800), (5100, 2550), (4100, 2050), (3410, 1700), (3399, 1690)]
)
def test_saver_price_is_half_rounded_down_to_ten_cents(standard, saver):
    from mcp_servers.travel_search import data

    price = data.saver_price(standard)
    assert price == saver
    assert isinstance(price, int) and price % 10 == 0


async def test_saver_fare_is_parseable_for_simulated_and_live_trains():
    from mcp_servers.travel_search import data

    for train_id in ("DR-BER-GVA-20261203-1002", "LV-BER-GVA-20261203-1034-IC1"):
        fares = await _call("get_fares", {"train_id": train_id})
        (saver,) = [f for f in fares if f["fare_type"] == "saver"]
        assert saver["fare_id"] == f"{train_id}-2nd-saver"
        assert saver["class_"] == "2nd"
        assert saver["refundable"] is False
        assert saver["price_cents"] == 2550
        assert data.get_fare(saver["fare_id"]) == saver
        hold = await _call("hold_seat", {"fare_id": saver["fare_id"], "passenger_ref": "pax-1"})
        assert hold["price_cents"] == 2550
    # A live train in the evening peak has no saver, so its saver id is unknown.
    with pytest.raises(ToolError, match="unknown fare"):
        await _call(
            "hold_seat", {"fare_id": "LV-BER-GVA-20261203-1734-IC1-2nd-saver", "passenger_ref": "p"}
        )
    with pytest.raises(data.UnknownItem):
        data.get_fare("DR-BER-GVA-20261203-1002-3rd")
