"""The UI's static copies of backend data stay equal to the backend.

* ``ui/src/network.ts`` LINKS (the trip planner's map) mirrors ``data.DURATIONS_MIN``.
* ``ui/src/fixtures/events.ts`` QUOTE_HASHES (the demo mode's POST /runs/{id}/quote) are the
  hashes the backend computes for the mock round trip's offer, selection by selection.
"""

import json
import re
from itertools import product
from pathlib import Path

from agents.graph import build_graph, initial_state
from agents.llm import MockLLM
from agents.state import SELECTED_RATIONALE, quote_for_selection
from agents.tools import GatedToolClient, make_clients
from controls.audit_log import AuditLog
from controls.policy import ToolPolicy
from mcp_servers.travel_search import data

REPO_ROOT = Path(__file__).resolve().parent.parent
NETWORK_TS = REPO_ROOT / "ui" / "src" / "network.ts"
EVENTS_TS = REPO_ROOT / "ui" / "src" / "fixtures" / "events.ts"
MOCK_FIXTURES = REPO_ROOT / "agents" / "fixtures" / "mock_roundtrip.json"


def test_planner_map_links_match_the_simulated_durations():
    source = NETWORK_TS.read_text(encoding="utf-8")
    block = source[source.index("export const LINKS") :]
    block = block[: block.index("];")]
    links = {
        frozenset({a, b}): int(minutes)
        for a, b, minutes in re.findall(r'\["([^"]+)", "([^"]+)", (\d+)\]', block)
    }
    assert links == data.DURATIONS_MIN

    stations = dict(re.findall(r'name: "([^"]+)", code: "([A-Z]{3})"', source))
    assert stations == data.STATIONS


async def test_demo_quote_hashes_match_the_backend_offer(tmp_path):
    source = EVENTS_TS.read_text(encoding="utf-8")
    block = source[source.index("export const QUOTE_HASHES") :]
    block = block[: block.index("};")]
    hashes = dict(re.findall(r'"([^"|]+\|[^"]+)": "([0-9a-f]{64})"', block))

    fixtures = json.loads(MOCK_FIXTURES.read_text(encoding="utf-8"))
    tools = GatedToolClient(
        make_clients(in_memory=True), ToolPolicy.from_yaml(), AuditLog(tmp_path / "a.jsonl")
    )
    graph = build_graph(MockLLM(fixtures), tools)
    state = await graph.ainvoke(
        initial_state("ui-sync", "Zurich to Bern, 1 adult."),
        {"configurable": {"thread_id": "ui-sync"}},
    )
    offer = state["offer"]

    # Every selection the demo can make, and nothing else.
    combos = {
        "|".join(o.fare_id for o in pick) for pick in product(*(leg.options for leg in offer.legs))
    }
    assert set(hashes) == combos
    for key, expected in hashes.items():
        selected = quote_for_selection(offer, key.split("|"), SELECTED_RATIONALE)
        assert selected.quote_hash == expected, key
    assert hashes["|".join(offer.recommended_fare_ids())] == state["quote"].quote_hash
