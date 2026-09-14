"""Unit tests for the framework-owned bottleneck-walk ledger.

The ledger (``vibesys.loops.agent.bottleneck_ledger``) is the deterministic walk
cursor for the ``dataflow_opt`` modality: it merges a ranked attribution into a
durable structure, selects the round's active component (honoring a bounded soft
override), and advances / exhausts a component on a measured plateau. These tests
cover the pure state machine plus the framework-owned attribution subprocess,
which is driven through a fake ``LoopContext`` backend rather than real valgrind.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from vibesys.loops.agent import bottleneck_ledger as bl

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.run.protocol import LoopContext


def _attr(*components: tuple[str, float]) -> dict:
    return {"components": [{"component": name, "pct": pct} for name, pct in components]}


class _FakeResult:
    """Stand-in for the judge backend's execute result."""

    def __init__(self, exit_code: int, output: str) -> None:
        self.exit_code = exit_code
        self.output = output


class _FakeBackend:
    """Records executed commands; returns a canned main result, then a benign
    cleanup result. Optionally raises on the first (main) execute call."""

    def __init__(self, main: _FakeResult | None = None, *, raise_on_main: bool = False) -> None:
        self._main = main
        self._raise_on_main = raise_on_main
        self.commands: list[str] = []

    def execute(self, command: str, timeout: int | None = None) -> _FakeResult:  # noqa: ARG002  # tracked: #288
        self.commands.append(command)
        if len(self.commands) == 1:
            if self._raise_on_main:
                raise RuntimeError("backend unavailable")  # noqa: TRY003  # tracked: #288
            assert self._main is not None
            return self._main
        return _FakeResult(0, "")  # cleanup call in the finally block


class _FakeCtx:
    """Minimal LoopContext surface used by ``run_attribution``."""

    def __init__(self, workspace: Path, backend: _FakeBackend) -> None:
        self.workspace = workspace
        self.judge_backend = backend
        self.logs: list[str] = []

    def lprint(self, message: str) -> None:
        self.logs.append(message)


def _framed(payload: str) -> str:
    """Wrap ``payload`` in the marker transport ``run_attribution`` parses."""
    return f"noise before\n{bl._ATTRIBUTION_MARKER}\n{payload}\n{bl._ATTRIBUTION_END_MARKER}\n"  # noqa: SLF001  # tracked: #288


def _make_script(tmp_path: Path) -> None:
    script = tmp_path / bl.ATTRIBUTION_SCRIPT_REL
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("# attribution stub\n")


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


# --------------------------------------------------------------------------- #
# Persistence + projection edge cases
# --------------------------------------------------------------------------- #


def test_load_ledger_backfills_missing_top_level_keys(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    # An older/hand-edited ledger with no version/active_component/components
    # keys must load into a complete structure rather than KeyError later.
    path = tmp_path / "bottlenecks.json"
    path.write_text("{}")
    ledger = bl.load_ledger(path)
    assert ledger == {"components": [], "active_component": None, "version": 1}


def test_repopulate_skips_unnamed_components():  # noqa: ANN201  # tracked: #288
    ledger = bl.new_ledger()
    attribution = {"components": [{"component": "", "pct": 9.0}, {"component": "a", "pct": 1.0}]}
    bl.repopulate_from_attribution(ledger, attribution, round_number=1)
    assert [c["name"] for c in ledger["components"]] == ["a"]


def test_advance_noops_when_active_component_unknown():  # noqa: ANN201  # tracked: #288
    ledger = bl.new_ledger()
    bl.repopulate_from_attribution(ledger, _attr(("a", 90.0)), round_number=1)
    # Naming a component absent from the ledger is a safe no-op.
    bl.advance_after_round(ledger, "ghost", round_number=1, passed=True, walk_metric=1.5)
    a = next(c for c in ledger["components"] if c["name"] == "a")
    assert a["rounds_spent"] == 0


# --------------------------------------------------------------------------- #
# Attribution subprocess (driven through a fake backend)
# --------------------------------------------------------------------------- #


def test_run_attribution_returns_none_when_script_absent(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    backend = _FakeBackend()
    ctx = _FakeCtx(tmp_path, backend)
    assert bl.run_attribution(cast("LoopContext", ctx), round_number=1) is None
    # The backend is never invoked when the bundle ships no attribution script.
    assert backend.commands == []
    assert any("not present" in m for m in ctx.logs)


def test_run_attribution_parses_marker_framed_json(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    _make_script(tmp_path)
    payload = '{"components": [{"component": "trace/implementations", "pct": 53.6}]}'
    backend = _FakeBackend(_FakeResult(0, _framed(payload)))
    ctx = _FakeCtx(tmp_path, backend)
    result = bl.run_attribution(cast("LoopContext", ctx), round_number=2)
    assert result == {"components": [{"component": "trace/implementations", "pct": 53.6}]}
    # Main command + cleanup in the finally block.
    assert len(backend.commands) == 2
    assert bl.ATTRIBUTION_SCRIPT_REL in backend.commands[0]


def test_run_attribution_returns_none_on_nonzero_exit(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    _make_script(tmp_path)
    backend = _FakeBackend(_FakeResult(2, _framed("{}")))
    ctx = _FakeCtx(tmp_path, backend)
    assert bl.run_attribution(cast("LoopContext", ctx), round_number=1) is None
    assert any("exited 2" in m for m in ctx.logs)


def test_run_attribution_returns_none_when_marker_missing(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    _make_script(tmp_path)
    backend = _FakeBackend(_FakeResult(0, "just some noise, no markers here"))
    ctx = _FakeCtx(tmp_path, backend)
    assert bl.run_attribution(cast("LoopContext", ctx), round_number=1) is None
    assert any("did not include its result JSON" in m for m in ctx.logs)


def test_run_attribution_returns_none_on_malformed_json(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    _make_script(tmp_path)
    backend = _FakeBackend(_FakeResult(0, _framed("{not valid json")))
    ctx = _FakeCtx(tmp_path, backend)
    assert bl.run_attribution(cast("LoopContext", ctx), round_number=1) is None
    assert any("malformed" in m for m in ctx.logs)


def test_run_attribution_returns_none_when_execute_raises(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    _make_script(tmp_path)
    backend = _FakeBackend(raise_on_main=True)
    ctx = _FakeCtx(tmp_path, backend)
    assert bl.run_attribution(cast("LoopContext", ctx), round_number=1) is None
    assert any("could not be executed" in m for m in ctx.logs)
