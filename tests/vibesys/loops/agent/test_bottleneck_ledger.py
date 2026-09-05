"""Unit tests for the framework-owned bottleneck-walk ledger.

The ledger (``vibesys.loops.agent.bottleneck_ledger``) is the deterministic walk
cursor for the ``dataflow_opt`` modality: it merges a ranked attribution into a
durable structure, selects the round's active component (honoring a bounded soft
override), and advances / exhausts a component on a measured plateau. These tests
cover the pure state machine; the valgrind and subprocess paths are not exercised
here.
"""

from __future__ import annotations

from vibesys.loops.agent import bottleneck_ledger as bl


def _attr(*components: tuple[str, float]) -> dict:
    return {"components": [{"component": name, "pct": pct} for name, pct in components]}


def test_new_and_roundtrip_persistence(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    path = tmp_path / "sub" / "bottlenecks.json"
    # Missing file loads a fresh ledger.
    ledger = bl.load_ledger(path)
    assert ledger == {"version": 1, "active_component": None, "components": []}
    bl.repopulate_from_attribution(ledger, _attr(("a", 50.0)), round_number=1)
    bl.save_ledger(path, ledger)
    reloaded = bl.load_ledger(path)
    assert [c["name"] for c in reloaded["components"]] == ["a"]


def test_repopulate_appends_new_and_orders_by_rank():  # noqa: ANN201  # tracked: #288
    ledger = bl.new_ledger()
    bl.repopulate_from_attribution(ledger, _attr(("a", 10.0), ("b", 90.0)), round_number=1)
    # Order follows attribution order (ranking is decided upstream), history seeded.
    assert [c["name"] for c in ledger["components"]] == ["a", "b"]
    for comp in ledger["components"]:
        assert comp["status"] == "open"
        assert len(comp["ir_pct_history"]) == 1
    # A second attribution reorders and appends a fresh history point.
    bl.repopulate_from_attribution(ledger, _attr(("b", 40.0), ("c", 5.0)), round_number=2)
    assert [c["name"] for c in ledger["components"]][:2] == ["b", "c"]
    b = next(c for c in ledger["components"] if c["name"] == "b")
    assert [h["pct"] for h in b["ir_pct_history"]] == [90.0, 40.0]


def test_repopulate_preserves_dropped_but_exhausted():  # noqa: ANN201  # tracked: #288
    ledger = bl.new_ledger()
    bl.repopulate_from_attribution(ledger, _attr(("a", 50.0), ("b", 50.0)), round_number=1)
    a = next(c for c in ledger["components"] if c["name"] == "a")
    a["status"] = "exhausted"
    # 'a' drops out of the new ranking, but its exhausted status must survive so
    # a re-ranking never resurrects a drained component.
    bl.repopulate_from_attribution(ledger, _attr(("b", 80.0)), round_number=2)
    names = {c["name"] for c in ledger["components"]}
    assert names == {"a", "b"}
    a = next(c for c in ledger["components"] if c["name"] == "a")
    assert a["status"] == "exhausted"


def test_select_picks_top_non_exhausted():  # noqa: ANN201  # tracked: #288
    ledger = bl.new_ledger()
    bl.repopulate_from_attribution(ledger, _attr(("a", 90.0), ("b", 10.0)), round_number=1)
    assert bl.select_active_component(ledger) == "a"
    assert ledger["active_component"] == "a"
    a = next(c for c in ledger["components"] if c["name"] == "a")
    assert a["status"] == "active"
    # Exhaust 'a'; selection advances to 'b' and demotes any stale active flag.
    a["status"] = "exhausted"
    assert bl.select_active_component(ledger) == "b"
    assert ledger["active_component"] == "b"


def test_select_returns_none_when_all_exhausted():  # noqa: ANN201  # tracked: #288
    ledger = bl.new_ledger()
    bl.repopulate_from_attribution(ledger, _attr(("a", 90.0)), round_number=1)
    ledger["components"][0]["status"] = "exhausted"
    assert bl.select_active_component(ledger) is None
    assert ledger["active_component"] is None


def test_override_honored_only_when_known_and_not_exhausted():  # noqa: ANN201  # tracked: #288
    ledger = bl.new_ledger()
    bl.repopulate_from_attribution(ledger, _attr(("a", 90.0), ("b", 10.0)), round_number=1)
    # Known, non-exhausted override wins over the top-ranked component.
    assert bl.select_active_component(ledger, override="b") == "b"
    # Unknown override falls back to the top-ranked non-exhausted component.
    assert bl.select_active_component(ledger, override="nope") == "a"
    # Exhausted override is ignored.
    b = next(c for c in ledger["components"] if c["name"] == "b")
    b["status"] = "exhausted"
    assert bl.select_active_component(ledger, override="b") == "a"


def test_advance_resets_on_improvement_and_exhausts_on_plateau():  # noqa: ANN201  # tracked: #288
    ledger = bl.new_ledger()
    bl.repopulate_from_attribution(ledger, _attr(("a", 90.0)), round_number=1)
    bl.select_active_component(ledger)
    # Round 1: first measured walk metric establishes the baseline (improvement).
    bl.advance_after_round(ledger, "a", round_number=1, passed=True, walk_metric=1.00)
    a = next(c for c in ledger["components"] if c["name"] == "a")
    assert a["rounds_spent"] == 1
    assert a["rounds_since_improvement"] == 0
    assert a["status"] == "active"
    # Round 2: a >=2% gain resets the plateau counter.
    bl.advance_after_round(ledger, "a", round_number=2, passed=True, walk_metric=1.05)
    assert a["rounds_since_improvement"] == 0
    assert a["status"] == "active"
    # Rounds 3-4: sub-threshold gains stack until the component exhausts at 2.
    bl.advance_after_round(ledger, "a", round_number=3, passed=True, walk_metric=1.055)
    assert a["rounds_since_improvement"] == 1
    assert a["status"] == "active"
    bl.advance_after_round(ledger, "a", round_number=4, passed=True, walk_metric=1.056)
    assert a["rounds_since_improvement"] == 2
    assert a["status"] == "exhausted"
    assert ledger["active_component"] is None


def test_advance_failed_round_counts_toward_exhaustion():  # noqa: ANN201  # tracked: #288
    ledger = bl.new_ledger()
    bl.repopulate_from_attribution(ledger, _attr(("a", 90.0)), round_number=1)
    bl.select_active_component(ledger)
    # A failed or unmeasured active round has no metric but still spends the
    # component so a stuck one eventually exhausts.
    bl.advance_after_round(ledger, "a", round_number=1, passed=False, walk_metric=None)
    bl.advance_after_round(ledger, "a", round_number=2, passed=True, walk_metric=None)
    a = next(c for c in ledger["components"] if c["name"] == "a")
    assert a["rounds_spent"] == 2
    assert a["status"] == "exhausted"


def test_advance_noops_without_active_component():  # noqa: ANN201  # tracked: #288
    ledger = bl.new_ledger()
    bl.repopulate_from_attribution(ledger, _attr(("a", 90.0)), round_number=1)
    # No active component named: nothing changes.
    bl.advance_after_round(ledger, "", round_number=1, passed=True, walk_metric=1.5)
    bl.advance_after_round(ledger, None, round_number=1, passed=True, walk_metric=1.5)
    a = next(c for c in ledger["components"] if c["name"] == "a")
    assert a["rounds_spent"] == 0


def test_ranked_bottlenecks_projection():  # noqa: ANN201  # tracked: #288
    attribution = {
        "components": [
            {"component": "trace/implementations", "pct": 42.5, "top_functions": ["merge_by"]},
            {"component": "", "pct": 1.0},  # dropped: no name
            {"component": "consolidation"},  # pct/top_functions default
        ]
    }
    ranked = bl.ranked_bottlenecks(attribution)
    assert [r["component"] for r in ranked] == ["trace/implementations", "consolidation"]
    assert ranked[0]["ir_pct"] == 42.5
    assert ranked[0]["top_functions"] == ["merge_by"]
    assert ranked[1]["ir_pct"] == 0.0
    assert ranked[1]["top_functions"] == []


def test_format_for_prompt_is_empty_when_no_components():  # noqa: ANN201  # tracked: #288
    assert bl.format_for_prompt(bl.new_ledger()) == ""


def test_format_for_prompt_renders_table():  # noqa: ANN201  # tracked: #288
    ledger = bl.new_ledger()
    bl.repopulate_from_attribution(ledger, _attr(("a", 12.5)), round_number=1)
    bl.select_active_component(ledger)
    text = bl.format_for_prompt(ledger)
    assert "component | status" in text
    assert "a | active" in text
    assert "12.50%" in text
