"""Framework-owned bottleneck-walk ledger for the ``dataflow_opt`` modality.

The ledger is the *framework-owned* walk cursor. A modality's attribution
capability (the ``dataflow_opt`` bundle's ``profiler/attribute_cpu.py``) writes a
canonical ranked ``attribution.json`` with a fixed component vocabulary; the loop
reads it into this ledger, which tracks each component's status
(``open`` → ``active`` → ``exhausted``), how many rounds have been spent on it,
and the best walk metric seen while it was active. The framework — not the LLM —
selects the round's ``active_component`` (top-ranked non-exhausted), though the
orchestrator may emit a soft override. All counters live in the JSON so the walk
is deterministic and resume-safe.

Nothing here imports the agent loop; the loop imports these helpers. The ledger
lives at ``log_dir/bottlenecks.json`` and the latest attribution at
``log_dir/attribution.json`` — both outside the git-tracked workspace, so the
walk state never pollutes the candidate history.
"""

from __future__ import annotations

import contextlib
import json
import shlex
import uuid
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from vibesys.run import LoopContext

# The relative path, inside the materialized workspace, of the attribution
# script shipped by a ``dataflow_opt`` bundle. This is the modality convention
# that lets the framework run attribution without a bespoke command channel.
ATTRIBUTION_SCRIPT_REL = "profiler/attribute_cpu.py"

# Component-scoped plateau: after this many *passed* active rounds without the
# walk metric improving by >= threshold pct, the component is marked exhausted
# and the cursor advances. Sibling to the loop's global ``_detect_plateau`` but
# per-component and counted on the ledger.
_COMPONENT_PLATEAU_MIN_ROUNDS = 2
_COMPONENT_PLATEAU_THRESHOLD_PCT = 2.0

# Marker/stdout transport for recovering the ranked JSON, mirroring
# ``gates.run_benchmark_gate`` so attribution works identically under a remote
# sandbox where the host cannot read the written file directly.
_ATTRIBUTION_MARKER = "__VIBESYS_ATTRIBUTION_BEGIN__"
_ATTRIBUTION_END_MARKER = "__VIBESYS_ATTRIBUTION_END__"
_ATTRIBUTION_TIMEOUT_S = 1800

_LEDGER_STATUSES = ("open", "active", "exhausted")


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def new_ledger() -> dict[str, Any]:
    """Return a fresh, empty ledger structure."""
    return {"version": 1, "active_component": None, "components": []}


def load_ledger(path: Path) -> dict[str, Any]:
    """Load the ledger from ``path``, tolerating a missing/older file."""
    if not path.exists():
        return new_ledger()
    data = json.loads(path.read_text())
    # Tolerate a hand-edited / older ledger: fill missing top-level keys.
    if "components" not in data:
        data["components"] = []
    if "active_component" not in data:
        data["active_component"] = None
    if "version" not in data:
        data["version"] = 1
    return data


def save_ledger(path: Path, ledger: dict[str, Any]) -> None:
    """Persist the ledger to ``path`` (creating the parent directory)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, indent=2))


def _ledger_component(ledger: dict[str, Any], name: str) -> dict[str, Any] | None:
    for comp in ledger["components"]:
        if comp.get("name") == name:
            return comp
    return None


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def run_attribution(ctx: LoopContext, *, round_number: int) -> dict[str, Any] | None:
    """Run the bundle's attribution script and return the ranked JSON.

    The framework — not an agent — runs attribution: the script profiles the
    *unmodified* candidate binary and emits a deterministic ranked component
    list. Returns the parsed ``{"components": [...]}`` object, or ``None`` when
    the script is absent, errors, or produces no recoverable JSON. Attribution
    only steers the soft walk cursor, so a failure degrades the round to
    cold-start planning rather than aborting it.
    """
    script = ctx.workspace / ATTRIBUTION_SCRIPT_REL
    if not script.exists():
        ctx.lprint(
            f"[ledger] attribution script {ATTRIBUTION_SCRIPT_REL} not present; "
            "skipping bottleneck ranking this round"
        )
        return None
    output_path = f"/tmp/vibesys-attribution-{round_number}-{uuid.uuid4().hex[:12]}.json"  # noqa: S108
    command = (
        f"rm -f -- {shlex.quote(output_path)}"
        f" && uv run python {shlex.quote(ATTRIBUTION_SCRIPT_REL)}"
        f" --output-json {shlex.quote(output_path)}"
        f" && printf '\\n{_ATTRIBUTION_MARKER}\\n'"
        f" && cat {shlex.quote(output_path)}"
        f" && printf '\\n{_ATTRIBUTION_END_MARKER}\\n'"
    )
    ctx.lprint(f"[ledger] running attribution: {ATTRIBUTION_SCRIPT_REL}")
    try:
        result = ctx.judge_backend.execute(command, timeout=_ATTRIBUTION_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001  # tracked: #288
        ctx.lprint(f"[ledger] attribution could not be executed: {exc}")
        return None
    finally:
        with contextlib.suppress(Exception):
            ctx.judge_backend.execute(f"rm -f -- {shlex.quote(output_path)}")
    if result.exit_code != 0:
        ctx.lprint(f"[ledger] attribution exited {result.exit_code}; skipping ranking this round")
        return None
    output = result.output
    _, marker, framed = output.rpartition(_ATTRIBUTION_MARKER)
    encoded, end_marker, _ = framed.partition(_ATTRIBUTION_END_MARKER)
    if not marker or not end_marker:
        ctx.lprint("[ledger] attribution output did not include its result JSON; skipping ranking")
        return None
    try:
        return json.loads(encoded.strip())
    except json.JSONDecodeError as exc:
        ctx.lprint(f"[ledger] attribution JSON was malformed: {exc}")
        return None


def ranked_bottlenecks(attribution: dict[str, Any]) -> list[dict[str, Any]]:
    """Project an attribution payload into ``RankedBottleneck``-shaped dicts."""
    out: list[dict[str, Any]] = []
    for entry in attribution.get("components", []) or []:
        name = entry.get("component")
        if not name:
            continue
        out.append(
            {
                "component": name,
                "ir_pct": float(entry.get("pct", 0.0) or 0.0),
                "top_functions": list(entry.get("top_functions", []) or []),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Walk cursor
# ---------------------------------------------------------------------------


def repopulate_from_attribution(
    ledger: dict[str, Any],
    attribution: dict[str, Any],
    *,
    round_number: int,
) -> dict[str, Any]:
    """Merge a fresh ``attribution.json`` into the ledger in place.

    New components are appended as ``open``; known components keep their
    ``status``/``rounds_spent``/plateau counters and just get a fresh
    ``ir_pct`` history point. The ledger's component order is refreshed to
    follow the attribution ranking (highest Ir first), which is what the walk
    cursor drains — but *exhausted* components keep their status so a
    re-ranking never resurrects a drained one.
    """
    attr_components = attribution.get("components", []) or []
    existing = {c["name"]: c for c in ledger["components"]}
    new_order: list[dict[str, Any]] = []
    for entry in attr_components:
        name = entry.get("component")
        if not name:
            continue
        pct = float(entry.get("pct", 0.0) or 0.0)
        comp: dict[str, Any] | None = existing.get(name)
        if comp is None:
            comp = {
                "name": name,
                "status": "open",
                "rounds_spent": 0,
                "best_metric_while_active": None,
                "rounds_since_improvement": 0,
                "ir_pct_history": [],
                "metric_history": [],
            }
        comp.setdefault("ir_pct_history", [])
        comp["ir_pct_history"].append({"round": round_number, "pct": pct})
        comp["latest_ir_pct"] = pct
        new_order.append(comp)
    # Preserve any known components that dropped out of the latest ranking
    # (e.g. optimized below the annotate threshold) so their history/status
    # aren't lost. Append them after the freshly-ranked ones.
    ranked_names = {c["name"] for c in new_order}
    new_order.extend(comp for comp in ledger["components"] if comp["name"] not in ranked_names)
    ledger["components"] = new_order
    return ledger


def select_active_component(
    ledger: dict[str, Any],
    *,
    override: str | None = None,
) -> str | None:
    """Choose the round's active component and mark it ``active``.

    Honors a valid ``override`` (a known, non-exhausted component) — the soft
    revisit/reorder escape hatch — otherwise picks the top-ranked non-exhausted
    component (ledger order == attribution ranking). Returns the chosen
    component name (also stored on ``ledger``), or ``None`` when every component
    is exhausted / the ledger is empty.
    """
    chosen: dict[str, Any] | None = None
    if override:
        cand = _ledger_component(ledger, override)
        if cand is not None and cand.get("status") != "exhausted":
            chosen = cand
    if chosen is None:
        for comp in ledger["components"]:
            if comp.get("status") != "exhausted":
                chosen = comp
                break
    if chosen is None:
        ledger["active_component"] = None
        return None
    # Demote any other component that was left marked active (e.g. after an
    # override switch) so exactly one component is active at a time.
    for comp in ledger["components"]:
        if comp is not chosen and comp.get("status") == "active":
            comp["status"] = "open"
    chosen["status"] = "active"
    ledger["active_component"] = chosen["name"]
    return chosen["name"]


def advance_after_round(
    ledger: dict[str, Any],
    active_component: str | None,
    *,
    round_number: int,
    passed: bool,
    walk_metric: float | None,
) -> None:
    """Update the active component's counters and exhaust it on plateau.

    On a component-scoped plateau the active component flips to ``exhausted``,
    advancing the walk cursor next round.

    ``walk_metric`` is a *higher-is-better* score (the loop feeds a CPU
    reduction ratio ``baseline / candidate``). Only *passed* rounds with a
    fresh ``walk_metric`` move the plateau counter; failed / metric-less rounds
    still increment ``rounds_spent`` so the walk doesn't stall silently on a
    component that can't be improved.
    """
    if not active_component:
        return
    comp = _ledger_component(ledger, active_component)
    if comp is None:
        return
    comp["rounds_spent"] = int(comp.get("rounds_spent", 0)) + 1
    if not (passed and walk_metric is not None):
        # A failed or un-measured active round counts toward giving up on the
        # component (so a stuck component eventually exhausts), but has no
        # metric to compare, so treat it as no-improvement.
        comp["rounds_since_improvement"] = int(comp.get("rounds_since_improvement", 0)) + 1
    else:
        comp.setdefault("metric_history", [])
        comp["metric_history"].append({"round": round_number, "walk_metric": walk_metric})
        best = comp.get("best_metric_while_active")
        improved = False
        if best is None or best <= 0:
            improved = True
        else:
            gain_pct = (walk_metric - best) / best * 100.0
            improved = gain_pct >= _COMPONENT_PLATEAU_THRESHOLD_PCT
        if improved:
            comp["best_metric_while_active"] = walk_metric
            comp["rounds_since_improvement"] = 0
        else:
            comp["rounds_since_improvement"] = int(comp.get("rounds_since_improvement", 0)) + 1
    if int(comp.get("rounds_since_improvement", 0)) >= _COMPONENT_PLATEAU_MIN_ROUNDS:
        comp["status"] = "exhausted"
        if ledger.get("active_component") == active_component:
            ledger["active_component"] = None


def format_for_prompt(ledger: dict[str, Any]) -> str:
    """Render the ledger as a compact table for the orchestrator prompt.

    Narration only; the JSON file remains authoritative.
    """
    components = ledger.get("components", [])
    if not components:
        return ""
    lines = ["component | status | rounds_spent | latest_ir_pct | best_metric"]
    for comp in components:
        pct = comp.get("latest_ir_pct")
        pct_str = f"{pct:.2f}%" if isinstance(pct, (int, float)) else "-"
        best = comp.get("best_metric_while_active")
        best_str = f"{best:.4f}" if isinstance(best, (int, float)) else "-"
        lines.append(
            f"{comp.get('name')} | {comp.get('status')} | "
            f"{comp.get('rounds_spent', 0)} | {pct_str} | {best_str}"
        )
    return "\n".join(lines)
