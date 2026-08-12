"""Tests for domain packs — the ``--domain`` pluggable-context mechanism.

Covers the resolver (built-in name / path / error), the section renderer
(present, empty, missing, ``single_agent`` derivation, context branching), and
end-to-end injection into the base prompts for both built-in domains
(``differential-dataflow`` carries the in-place superoptimization prose;
``generic`` injects nothing of its own).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vibe_database.loops.agent.domain import (
    DEFAULT_DOMAIN,
    DOMAIN_ROLES,
    builtin_domains,
    render_domain_section,
    resolve_domain,
)
from vibe_database.prompts import render_template

_TEMPLATE_DIR = (
    Path(__file__).resolve().parents[3] / "src" / "vibe_database" / "loops" / "agent" / "templates"
)

_DOMAIN = "differential-dataflow"
_MODALITY = "dataflow-opt"


# --------------------------------------------------------------------------- #
# resolver
# --------------------------------------------------------------------------- #
def test_builtins_present():
    names = builtin_domains()
    assert "differential-dataflow" in names
    assert "generic" in names
    assert "nexmark" not in names  # nexmark synthesis targets were removed
    assert "nexmark-q7" not in names
    assert "llm-serving" not in names  # LLM-serving domain was removed in the fork
    assert "streaming-ivm" not in names  # streaming-ivm target was removed
    assert "README" not in names  # the authoring guide is not a domain
    assert DEFAULT_DOMAIN == "differential-dataflow"


def test_resolve_builtin_name():
    d = resolve_domain(_DOMAIN)
    assert d.is_file()
    assert d.name == "differential-dataflow.md"


def test_resolve_path(tmp_path: Path):
    f = tmp_path / "mine.md"
    f.write_text("# Mine\n\n## implementer\nhello\n")
    d = resolve_domain(str(f))
    assert d == f.resolve()


def test_resolve_unknown_raises():
    with pytest.raises(ValueError) as exc:
        resolve_domain("does-not-exist-xyz")
    # error lists the built-ins to guide the user
    assert "differential-dataflow" in str(exc.value)


# --------------------------------------------------------------------------- #
# section renderer
# --------------------------------------------------------------------------- #
def test_render_missing_role_is_empty(tmp_path: Path):
    # a domain file with no matching ## <role> section injects nothing
    f = tmp_path / "d.md"
    f.write_text("# Just docs, no role sections\n")
    assert render_domain_section(f, "implementer") == ""


def test_render_empty_role_is_empty():
    # generic.md has no role sections, so every role injects nothing
    d = resolve_domain("generic")
    for role in ("implementer", "judge", "single_agent"):
        assert render_domain_section(d, role) == ""


def test_render_differential_dataflow_has_content():
    d = resolve_domain(_DOMAIN)
    impl = render_domain_section(d, "implementer", modality=_MODALITY, reference_path="/ref")
    assert impl  # non-empty
    # leading/trailing blank lines are stripped — base template owns spacing
    assert impl == impl.strip("\n")
    # the body keeps its superoptimization prose
    assert "micro-optimiz" in impl.lower()
    assert "byte-identical" in impl.lower()


def test_deeper_markdown_heading_does_not_delimit_role(tmp_path: Path):
    """Only exact ``## <role>`` headings split sections."""
    f = tmp_path / "d.md"
    f.write_text(
        "# D\n\n"
        "## implementer\n"
        "IMPL-BEFORE\n\n"
        "### judge\n"
        "This is an implementer subsection, not the judge role.\n\n"
        "IMPL-AFTER\n\n"
        "## judge\n"
        "JUDGE-BODY\n"
    )

    impl = render_domain_section(f, "implementer")
    judge = render_domain_section(f, "judge")

    assert "IMPL-BEFORE" in impl
    assert "### judge" in impl
    assert "IMPL-AFTER" in impl
    assert judge == "JUDGE-BODY"


def test_render_role_branches_on_context(tmp_path: Path):
    """A role section rendered with bench_path set should reference it."""
    f = tmp_path / "d.md"
    f.write_text(
        "# D\n\n## judge\n{% if bench_path %}Benchmark lives at {{ bench_path }}.{% endif %}\n"
    )
    with_bench = render_domain_section(f, "judge", modality=_MODALITY, bench_path="/BENCHX")
    without_bench = render_domain_section(f, "judge", modality=_MODALITY, bench_path=None)
    assert "/BENCHX" in with_bench
    assert "/BENCHX" not in without_bench


def test_single_agent_derives_from_implementer_and_judge(tmp_path: Path):
    # no ## single_agent section -> derived from implementer + judge
    f = tmp_path / "d.md"
    f.write_text("# D\n\n## implementer\nIMPL-BODY\n\n## judge\nJUDGE-BODY\n")
    sa = render_domain_section(f, "single_agent")
    assert "IMPL-BODY" in sa
    assert "JUDGE-BODY" in sa


def test_differential_dataflow_single_agent_derives_from_impl_and_judge():
    # differential-dataflow.md ships no ## single_agent section, so it is derived
    # from the implementer + judge bodies.
    d = resolve_domain(_DOMAIN)
    sa = render_domain_section(d, "single_agent", modality=_MODALITY, reference_path="/ref")
    assert sa
    assert "micro-optimiz" in sa.lower()  # implementer-body optimization language
    assert "output-equivalence" in sa.lower()  # judge-body correctness-gate language


# --------------------------------------------------------------------------- #
# end-to-end injection into base prompts
# --------------------------------------------------------------------------- #
def _render_implementer(domain: str) -> str:
    d = resolve_domain(domain)
    section = render_domain_section(d, "implementer", modality=_MODALITY, reference_path="/ref")
    return render_template(
        "implementer_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        modality=_MODALITY,
        domain_implementer=section,
        task="TASK",
        pass_criteria="PC",
        reference_path="/ref",
        runtime_notes="",
        feedback=None,
    )


def test_differential_dataflow_injects_into_implementer():
    out = _render_implementer(_DOMAIN)
    # domain-pack prose is present
    assert "micro-optimization" in out.lower()
    assert "## Progress tracking" in out  # base skeleton intact


def test_generic_injects_nothing_extra():
    generic = _render_implementer("generic")
    # the generic render must be strictly shorter than differential-dataflow's,
    # which carries the injected domain section.
    dataflow = _render_implementer(_DOMAIN)
    assert len(generic) < len(dataflow)
    assert "## Progress tracking" in generic  # base skeleton intact


def test_no_triple_blank_at_injection_point():
    """Generic (empty injection) must not leave a triple newline gap."""
    out = _render_implementer("generic")
    # The injection point itself ({% if %}...{% endif %}) must collapse cleanly.
    # Locate the workspace->progress transition that brackets the injection.
    idx = out.index("## Progress tracking")
    window = out[max(0, idx - 6) : idx]
    assert "\n\n\n" not in window


# --------------------------------------------------------------------------- #
# orchestrator role
# --------------------------------------------------------------------------- #
def test_orchestrator_is_a_domain_role():
    assert "orchestrator" in DOMAIN_ROLES


def _render_orchestrator(domain: str) -> str:
    section = render_domain_section(resolve_domain(domain), "orchestrator", modality=_MODALITY)
    return render_template(
        "orchestrator_plan_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        objective="OBJ",
        profiler_summary=None,
        regression_info=None,
        exhaustion_info=None,
        roadmap_text="ROADMAP",
        plateau_warning=None,
        domain_orchestrator=section,
        runtime_notes="",
        env_kind="local",
    )


def test_differential_dataflow_provides_orchestrator_sequencing():
    section = render_domain_section(resolve_domain(_DOMAIN), "orchestrator", modality=_MODALITY)
    assert "output stays byte-identical and CPU follows" in section
    assert "micro-optimization" in section.lower()


def test_differential_dataflow_orchestrator_section_injected_into_plan():
    out = _render_orchestrator(_DOMAIN)
    assert "output stays byte-identical and CPU follows" in out
    # the back-reference resolves when a domain orchestrator section is provided
    assert "the optimization-floor section below" in out


def test_generic_orchestrator_has_no_domain_section():
    out = _render_orchestrator("generic")
    # the domain section is gone, and its back-reference collapses
    assert "output stays byte-identical and CPU follows" not in out
    assert "the optimization-floor section below" not in out
    assert "## Task granularity" in out  # base skeleton intact
