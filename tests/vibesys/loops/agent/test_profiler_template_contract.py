"""Over-supply contract for the profiler template family.

``_run_profiler`` (``src/vibesys/loops/agent/loop.py``) is a single call site
that selects among ``profilers/*.j2`` by ``profiler_kind`` and renders
whichever one gets selected with the same fixed set of kwargs — so a
variable this call always passes but a given variant never references is
silently dropped from the agent's prompt. Jinja discards an unused kwarg by
design; no undefined-variable check, strict or not, can see this direction.
``TemplateRenderer.unused_kwargs`` does: for one render call, it returns
exactly which of the passed kwargs the target template never reads (via
static analysis, so it also catches a key used only inside a branch this
particular call wouldn't take).

This needs no declared or derived "required" set — it only compares
``_run_profiler``'s own fixed kwargs against what each template actually
reads. A variant with a reviewed, genuine reason to ignore one of them marks
it with ``{# vs-prompts:unused: <var> - <reason> #}`` (mirroring
``FragmentFamily``'s empty-file convention for a deliberate skip); everything
else is a real gap this test fails on.

Caught two live bugs so far:
- ``nsys.j2``, ``neuron.j2``, ``macos_cpu.j2`` silently dropped
  ``runtime_notes`` (environment execution rules) while their siblings
  surfaced it — fixed by adding the block, not by marking it skipped.
- ``linux_cpu.j2`` silently dropped ``modality``, orphaning
  ``_modality/kv_store/profiler.j2`` (RESP2-specific py-spy/perf/strace
  guidance) even though LINUX_CPU is exactly the profiler kind
  ``examples/kv-store`` pairs with that modality — fixed the same way.

Every other gap this test would otherwise flag is marked skipped with a
reason tracing back to why: mostly that ``profile_execution`` can only ever
be ``"remote"`` for the Modal environment, which structurally never selects
any profiler kind besides TORCH/AUTO/NONE (see the markers in each ``.j2``
file for the exact backend/environment evidence).
"""

from __future__ import annotations

from vibesys.prompts import PROMPTS_DIR
from vibesys.prompts.renderer import _build_env
from vs_prompts.api import filter_skip_marked

_PROFILERS_DIR = PROMPTS_DIR / "loops" / "agent" / "profilers"
_AGENT_LOOP_ROOT = PROMPTS_DIR / "loops" / "agent"

# Exactly the kwargs _run_profiler (src/vibesys/loops/agent/loop.py:834-846)
# passes to whichever profiler template profiler_kind resolves to. Keep this
# in sync with that call site, not with any individual template.
_RUN_PROFILER_KWARGS: dict[str, object] = {
    "profile_focus": "General bottleneck analysis.",
    "benchmark_command": "uv run python benchmark/benchmark.py",
    "modality": "text_generation",
    "domain_profiler": "",
    "runtime_notes": "Runtime note: local isolated workspace.",
    "profile_execution": "local",
    "objective": "OBJECTIVE: maximize throughput.",
    "profiler_support_name": "example_profiler",
    "profiler_mcp_name": "vibesys-example-profiler",
}


def test_profiler_variants_use_every_kwarg_run_profiler_passes() -> None:
    paths = sorted(p for p in _PROFILERS_DIR.glob("*.j2") if not p.name.startswith("_"))
    assert paths, f"no profiler templates found under {_PROFILERS_DIR}"

    renderer = _build_env(_AGENT_LOOP_ROOT)
    failures = []
    for path in paths:
        name = f"profilers/{path.name}"
        unused = renderer.unused_kwargs(name, **_RUN_PROFILER_KWARGS)
        still_unused = filter_skip_marked(path, unused)
        if still_unused:
            failures.append(f"{path.name}: silently drops {sorted(still_unused)}")

    assert not failures, "\n".join(failures)
