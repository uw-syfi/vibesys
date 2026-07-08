"""Tests for registered domains — the ``--domain`` pluggable-context mechanism.

Covers the resolver (registered name / error), the role-file renderer (present,
empty, missing, ``single_agent`` derivation, context branching), and end-to-end
injection into the base prompts for both registered domains (``llm-serving``
carries serving prose; ``generic`` injects nothing of its own).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vibe_serve.domains.base import (
    DEFAULT_DOMAIN,
    DOMAIN_ROLES,
    DomainDefinition,
    DomainName,
    DomainRole,
)
from vibe_serve.domains.environment import NoopEnvironmentHooks
from vibe_serve.domains.llm_serving.hooks import LLMServingEnvironmentHooks
from vibe_serve.domains.registry import (
    DOMAINS,
    registered_domains,
    resolve_domain,
)
from vibe_serve.domains.rendering import render_domain_section
from vibe_serve.prompts import render_template

_TEMPLATE_DIR = (
    Path(__file__).resolve().parents[3] / "src" / "vibe_serve" / "loops" / "agent" / "templates"
)


def _temporary_domain(prompt_dir: Path) -> DomainDefinition:
    return DomainDefinition(
        name=DomainName.GENERIC,
        prompt_dir=prompt_dir,
        environment_hooks=NoopEnvironmentHooks(),
    )


# --------------------------------------------------------------------------- #
# resolver
# --------------------------------------------------------------------------- #
def test_registered_domains_present():
    names = registered_domains()
    assert "llm-serving" in names
    assert "generic" in names
    assert "README" not in names  # the authoring guide is not a domain
    assert DEFAULT_DOMAIN is DomainName.LLM_SERVING


def test_resolve_registered_name():
    d = resolve_domain(DomainName.LLM_SERVING)
    assert d.name is DomainName.LLM_SERVING
    assert d.prompt_dir.is_dir()
    assert d.prompt_dir.name == "templates"
    assert d.prompt_dir.parent.name == "llm_serving"


def test_resolve_path_is_not_supported(tmp_path: Path):
    f = tmp_path / "mine"
    f.mkdir()
    (f / "implementer.md").write_text("hello\n")
    with pytest.raises(TypeError, match="DomainName"):
        resolve_domain(str(f))


def test_registered_domains_carry_environment_hooks():
    assert isinstance(DOMAINS[DomainName.LLM_SERVING].environment_hooks, LLMServingEnvironmentHooks)
    assert isinstance(DOMAINS[DomainName.GENERIC].environment_hooks, NoopEnvironmentHooks)


def test_resolve_unknown_raises():
    with pytest.raises(TypeError) as exc:
        resolve_domain("does-not-exist-xyz")
    assert "DomainName" in str(exc.value)


# --------------------------------------------------------------------------- #
# role-file renderer
# --------------------------------------------------------------------------- #
def test_render_missing_role_is_empty(tmp_path: Path):
    # a domain directory with no matching <role>.md file injects nothing
    domain_dir = tmp_path / "domain"
    domain_dir.mkdir()
    (domain_dir / "README.md").write_text("# Just docs, no role files\n")
    assert render_domain_section(_temporary_domain(domain_dir), DomainRole.IMPLEMENTER) == ""


def test_render_empty_role_is_empty():
    # generic has no role files, so every role injects nothing
    d = resolve_domain(DomainName.GENERIC)
    for role in (DomainRole.IMPLEMENTER, DomainRole.JUDGE, DomainRole.SINGLE_AGENT):
        assert render_domain_section(d, role) == ""


def test_render_llm_serving_has_content():
    d = resolve_domain(DomainName.LLM_SERVING)
    impl = render_domain_section(
        d, DomainRole.IMPLEMENTER, modality="text_generation", reference_path="/ref"
    )
    assert impl  # non-empty
    # leading/trailing blank lines are stripped — base template owns spacing
    assert impl == impl.strip("\n")
    # the body keeps its own ## sub-headings (not treated as role delimiters)
    assert "## Required:" in impl


def test_role_file_keeps_markdown_headings(tmp_path: Path):
    """Role files are normal Markdown; headings inside them are preserved."""
    domain_dir = tmp_path / "domain"
    domain_dir.mkdir()
    (domain_dir / "implementer.md").write_text(
        "IMPL-BEFORE\n\n"
        "## Required:\n"
        "This is an implementer subsection, not a role delimiter.\n\n"
        "### judge\n"
        "This is an implementer subsection, not the judge role.\n\n"
        "IMPL-AFTER\n"
    )
    (domain_dir / "judge.md").write_text("JUDGE-BODY\n")

    d = _temporary_domain(domain_dir)
    impl = render_domain_section(d, DomainRole.IMPLEMENTER)
    judge = render_domain_section(d, DomainRole.JUDGE)

    assert "IMPL-BEFORE" in impl
    assert "## Required:" in impl
    assert "### judge" in impl
    assert "IMPL-AFTER" in impl
    assert judge == "JUDGE-BODY"


def test_render_role_branches_on_context():
    """A role file rendered with bench_path set should reference it."""
    d = resolve_domain(DomainName.LLM_SERVING)
    with_bench = render_domain_section(
        d, DomainRole.JUDGE, modality="text_generation", bench_path="/BENCHX"
    )
    without_bench = render_domain_section(
        d, DomainRole.JUDGE, modality="text_generation", bench_path=None
    )
    assert "/BENCHX" in with_bench
    assert "/BENCHX" not in without_bench


def test_render_role_branches_on_interface(tmp_path: Path):
    """`interface` reaches domain role files, so a language-agnostic pack can drop
    its in-process/Python-only gates under `--interface service`."""
    domain_dir = tmp_path / "domain"
    domain_dir.mkdir()
    (domain_dir / "judge.md").write_text(
        '{% if interface != "service" %}IN_PROCESS_GATE{% endif %}\n'
    )
    d = _temporary_domain(domain_dir)
    inprocess = render_domain_section(d, DomainRole.JUDGE, interface="inprocess")
    service = render_domain_section(d, DomainRole.JUDGE, interface="service")
    assert "IN_PROCESS_GATE" in inprocess
    assert "IN_PROCESS_GATE" not in service


def test_single_agent_uses_explicit_section_when_present():
    # llm-serving ships a bespoke single_agent.md file
    d = resolve_domain(DomainName.LLM_SERVING)
    sa = render_domain_section(
        d, DomainRole.SINGLE_AGENT, modality="text_generation", reference_path="/ref"
    )
    assert "do not let yourself cheat" in sa  # text unique to that section


def test_single_agent_derives_from_implementer_and_judge(tmp_path: Path):
    # no single_agent.md -> derived from implementer + judge
    domain_dir = tmp_path / "domain"
    domain_dir.mkdir()
    (domain_dir / "implementer.md").write_text("IMPL-BODY\n")
    (domain_dir / "judge.md").write_text("JUDGE-BODY\n")
    sa = render_domain_section(_temporary_domain(domain_dir), DomainRole.SINGLE_AGENT)
    assert "IMPL-BODY" in sa
    assert "JUDGE-BODY" in sa


# --------------------------------------------------------------------------- #
# end-to-end injection into base prompts
# --------------------------------------------------------------------------- #
def _render_implementer(domain: DomainName) -> str:
    d = resolve_domain(domain)
    section = render_domain_section(
        d, DomainRole.IMPLEMENTER, modality="text_generation", reference_path="/ref"
    )
    return render_template(
        "implementer_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        modality="text_generation",
        domain_implementer=section,
        task="TASK",
        pass_criteria="PC",
        reference_path="/ref",
        runtime_notes="",
        feedback=None,
    )


def test_llm_serving_injects_into_implementer():
    out = _render_implementer(DomainName.LLM_SERVING)
    # serving-specific prose from the domain package is present
    assert "serving" in out.lower()
    assert "## Progress tracking" in out  # base skeleton intact


def test_generic_injects_nothing_extra():
    generic = _render_implementer(DomainName.GENERIC)
    # the only serving refs left are from the modality include, not the domain;
    # the generic render must be strictly shorter than llm-serving's.
    serving = _render_implementer(DomainName.LLM_SERVING)
    assert len(generic) < len(serving)
    assert "## Progress tracking" in generic  # base skeleton intact


def test_no_triple_blank_at_injection_point():
    """Generic (empty injection) must not leave a triple newline gap."""
    out = _render_implementer(DomainName.GENERIC)
    # The injection point itself ({% if %}...{% endif %}) must collapse cleanly.
    # Locate the workspace->progress transition that brackets the injection.
    idx = out.index("## Progress tracking")
    window = out[max(0, idx - 6) : idx]
    assert "\n\n\n" not in window


# --------------------------------------------------------------------------- #
# orchestrator role
# --------------------------------------------------------------------------- #
def test_orchestrator_is_a_domain_role():
    assert DomainRole.ORCHESTRATOR in DOMAIN_ROLES


def _render_orchestrator(domain: DomainName) -> str:
    section = render_domain_section(
        resolve_domain(domain), DomainRole.ORCHESTRATOR, modality="text_generation"
    )
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


def test_llm_serving_provides_orchestrator_optimization_floor():
    section = render_domain_section(
        resolve_domain(DomainName.LLM_SERVING), DomainRole.ORCHESTRATOR, modality="text_generation"
    )
    assert "Optimization priority" in section
    assert "Continuous batching" in section


def test_llm_serving_orchestrator_floor_injected_into_plan():
    out = _render_orchestrator(DomainName.LLM_SERVING)
    assert "Optimization priority" in out
    # the line-39 back-reference resolves when a floor is provided
    assert "the optimization-floor section below" in out


def test_generic_orchestrator_has_no_llm_floor():
    out = _render_orchestrator(DomainName.GENERIC)
    # the prescriptive LLM floor is gone, and its back-reference collapses
    assert "Optimization priority" not in out
    assert "Continuous batching" not in out
    assert "the optimization-floor section below" not in out
    assert "## Task granularity" in out  # base skeleton intact
