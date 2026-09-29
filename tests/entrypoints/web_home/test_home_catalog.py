from __future__ import annotations

from typing import TYPE_CHECKING, get_args

from entrypoints.cli.constants import _OUTER_LOOPS
from entrypoints.web_home.contract import OuterLoop
from vibesys.api import ComputeBackend
from vs_agent.api import SHIPPED_PROVIDERS

if TYPE_CHECKING:
    from tests.entrypoints.web_home.support import Home


def test_catalog_offers_every_cli_loop_with_its_budget_flag(home: Home) -> None:
    catalog = home.get("/api/agents/catalog").json()
    loops = {loop["id"]: loop for loop in catalog["outer_loops"]}

    assert list(loops) == list(_OUTER_LOOPS) == list(get_args(OuterLoop))
    assert loops["agent"]["budget"] == {"flag": "--max-rounds", "default": 24}
    assert loops["plain"]["budget"] == {"flag": "--max-rounds", "default": 5}
    assert loops["evolve"]["budget"] == {"flag": "--max-generations", "default": 8}
    assert loops["dynamic"]["budget"]["flag"] == "--max-rounds"
    assert loops["profile-guided"]["requires_profile_guided"] is True
    assert "implementer" in loops["agent"]["roles"]


def test_catalog_lists_shipped_providers_drivers_and_backends(home: Home) -> None:
    catalog = home.get("/api/agents/catalog").json()

    assert [p["provider"] for p in catalog["providers"]] == list(SHIPPED_PROVIDERS)
    codex = next(p for p in catalog["providers"] if p["provider"] == "codex")
    assert "gpt-5.5" in codex["suggested_models"]
    assert {d["driver"] for d in catalog["drivers"]} == {"agentshim", "omnigent"}
    assert catalog["compute_backends"] == [b.value for b in ComputeBackend]
    assert catalog["default_compute_backend"] in catalog["compute_backends"]
