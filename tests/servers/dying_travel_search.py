"""travel_search stdio server that exits or stalls mid-call: fixture for transport failures.

The first call of the tool named in ``DIE_ON_TOOL`` kills the process while the request is
pending (no response is ever sent); the first call of the tool named in ``STALL_ON_TOOL``
hangs instead (the process stays alive but never answers). ``DIE_MARKER`` records that it
already happened, so the fresh server started by the gate answers normally.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from fastmcp import FastMCP

from mcp_servers.travel_search import data

mcp = FastMCP("travel_search")


def _maybe_die(tool: str) -> None:
    marker = Path(os.environ["DIE_MARKER"])
    if os.environ.get("DIE_ON_TOOL") == tool and not marker.exists():
        marker.write_text("died")
        os._exit(1)
    if os.environ.get("STALL_ON_TOOL") == tool and not marker.exists():
        marker.write_text("stalled")
        time.sleep(300)


@mcp.tool
def search_trains(origin: str, destination: str, date: str) -> list[dict]:
    _maybe_die("search_trains")
    from datetime import date as date_type

    return data.search_trains(origin, destination, date_type.fromisoformat(date))


@mcp.tool
def get_fares(train_id: str) -> list[dict]:
    _maybe_die("get_fares")
    return data.get_fares(train_id)


@mcp.tool
def hold_seat(fare_id: str, passenger_ref: str) -> dict:
    _maybe_die("hold_seat")
    return data.hold_seat(fare_id, passenger_ref)


if __name__ == "__main__":
    mcp.run(show_banner=False)
