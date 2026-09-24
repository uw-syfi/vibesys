"""Tests for registered domains — the manifest-selected pluggable context.

Covers the resolver (registered name / error), the role-file renderer (present,
empty, missing, ``single_agent`` derivation, context branching), and end-to-end
injection into the base prompts for both registered domains (``llm-serving``
carries serving prose; ``generic`` injects nothing of its own).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from vibesys.constants import DomainName
from vibesys.domains.base import DOMAIN_ROLES, DomainDefinition, DomainRole
from vibesys.domains.environment import NoopEnvironmentHooks
from vibesys.domains.llm_serving.hooks import LLMServingEnvironmentHooks
from vibesys.domains.registry import (
    DOMAINS,
    registered_domains,
    resolve_domain,
)
from vibesys.domains.rendering import render_domain_section
from vibesys.prompts import PROMPTS_DIR, render_template

if TYPE_CHECKING:
    from pathlib import Path
_TEMPLATE_DIR = PROMPTS_DIR / "loops" / "agent"


def _temporary_domain(prompt_dir: Path) -> DomainDefinition:
    return DomainDefinition(
        name=DomainName.GENERIC,
        prompt_dir=prompt_dir,
        environment_hooks=NoopEnvironmentHooks(),
    )


# --------------------------------------------------------------------------- #
# resolver
# --------------------------------------------------------------------------- #
def test_registered_domains_present() -> None:
    names = registered_domains()
    assert "llm-serving" in names
    assert "generic" in names
    assert "microservices" in names
    assert "database" in names
    assert "README" not in names  # the authoring guide is not a domain


def test_resolve_registered_name() -> None:
    d = resolve_domain(DomainName.LLM_SERVING)
    assert d.name is DomainName.LLM_SERVING
    assert d.prompt_dir.is_dir()
    assert d.prompt_dir.name == "llm_serving"
    assert d.prompt_dir.parent.name == "domains"


def test_resolve_microservices_domain() -> None:
    d = resolve_domain(DomainName.MICROSERVICES)
    assert d.name is DomainName.MICROSERVICES
    assert d.prompt_dir.is_dir()
    assert d.prompt_dir.name == "microservices"
    assert d.prompt_dir.parent.name == "domains"


def test_resolve_database_domain() -> None:
    d = resolve_domain(DomainName.DATABASE)
    assert d.name is DomainName.DATABASE
    assert d.prompt_dir.is_dir()
    assert d.prompt_dir.name == "database"
    assert d.prompt_dir.parent.name == "domains"


def test_resolve_path_is_not_supported(tmp_path: Path) -> None:
    f = tmp_path / "mine"
    f.mkdir()
    (f / "implementer.md").write_text("hello\n")
    # The runtime guard is the subject here, so the declared type is violated
    # deliberately: a path string must not resolve as a domain.
    with pytest.raises(TypeError, match="DomainName"):
        resolve_domain(cast("DomainName", str(f)))


def test_registered_domains_carry_environment_hooks() -> None:
    assert isinstance(DOMAINS[DomainName.LLM_SERVING].environment_hooks, LLMServingEnvironmentHooks)
    assert isinstance(DOMAINS[DomainName.GENERIC].environment_hooks, NoopEnvironmentHooks)
    assert isinstance(DOMAINS[DomainName.MICROSERVICES].environment_hooks, NoopEnvironmentHooks)
    assert isinstance(DOMAINS[DomainName.DATABASE].environment_hooks, NoopEnvironmentHooks)


def test_domains_declare_torch_profiler_compatibility() -> None:
    assert DOMAINS[DomainName.LLM_SERVING].supports_torch_profiler
    assert not DOMAINS[DomainName.GENERIC].supports_torch_profiler
    assert not DOMAINS[DomainName.MICROSERVICES].supports_torch_profiler
    assert not DOMAINS[DomainName.DATABASE].supports_torch_profiler


def test_resolve_unknown_raises() -> None:
    # The runtime guard is the subject here, so the declared type is violated
    # deliberately: an unregistered name must not resolve.
    with pytest.raises(TypeError) as exc:
        resolve_domain(cast("DomainName", "does-not-exist-xyz"))
    assert "DomainName" in str(exc.value)


# --------------------------------------------------------------------------- #
# role-file renderer
# --------------------------------------------------------------------------- #
def test_render_missing_role_is_empty(tmp_path: Path) -> None:
    # a domain directory with no matching <role>.md file injects nothing
    domain_dir = tmp_path / "domain"
    domain_dir.mkdir()
    (domain_dir / "README.md").write_text("# Just docs, no role files\n")
    assert render_domain_section(_temporary_domain(domain_dir), DomainRole.IMPLEMENTER) == ""


def test_render_empty_role_is_empty() -> None:
    # generic has no role files, so every role injects nothing
    d = resolve_domain(DomainName.GENERIC)
    for role in (DomainRole.IMPLEMENTER, DomainRole.JUDGE, DomainRole.SINGLE_AGENT):
        assert render_domain_section(d, role) == ""


def test_render_llm_serving_has_content() -> None:
    d = resolve_domain(DomainName.LLM_SERVING)
    impl = render_domain_section(
        d,
        DomainRole.IMPLEMENTER,
        modality="text_generation",
        reference_path="/ref",
        workspace_sources=(),
    )
    assert impl  # non-empty
    # leading/trailing blank lines are stripped — base template owns spacing
    assert impl == impl.strip("\n")
    # the body keeps its own ## sub-headings (not treated as role delimiters)
    assert "## Use references as implementation support" in impl


def test_render_microservices_has_content() -> None:
    d = resolve_domain(DomainName.MICROSERVICES)
    impl = render_domain_section(d, DomainRole.IMPLEMENTER, interface="service")
    judge = render_domain_section(
        d,
        DomainRole.JUDGE,
        accuracy_command="./check",
        benchmark_command="./bench",
    )
    assert "microservice system" in impl
    assert "connection pools" in impl
    assert "./check" in judge
    assert "./bench" in judge


def test_render_database_has_content() -> None:
    d = resolve_domain(DomainName.DATABASE)
    impl = render_domain_section(d, DomainRole.IMPLEMENTER, reference_path="/ref")
    judge = render_domain_section(
        d,
        DomainRole.JUDGE,
        accuracy_command="./check",
        benchmark_command="./bench",
    )
    # in-place-superopt framing is present on both role files
    assert "database / dataflow engine" in impl
    assert "output-equivalence" in impl.lower()
    assert "output-equivalence" in judge.lower()
    assert "no rearchitecture" in judge.lower()
    # judge.md branches on the framework-supplied command variables
    assert "./check" in judge
    assert "./bench" in judge


def test_render_database_judge_omits_commands_when_absent() -> None:
    # with no commands supplied, the gated command lines drop out cleanly
    d = resolve_domain(DomainName.DATABASE)
    judge = render_domain_section(
        d, DomainRole.JUDGE, accuracy_command=None, benchmark_command=None
    )
    assert judge  # the always-on correctness prose still renders
    assert "./check" not in judge
    assert "./bench" not in judge


def test_role_file_keeps_markdown_headings(tmp_path: Path) -> None:
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


def test_llm_serving_judge_does_not_duplicate_framework_benchmark() -> None:
    """The LLM judge audits evidence instead of rerunning a trusted gate."""
    d = resolve_domain(DomainName.LLM_SERVING)
    with_bench = render_domain_section(
        d,
        DomainRole.JUDGE,
        modality="text_generation",
        benchmark_command="./BENCHX",
        workspace_sources=(),
    )
    without_bench = render_domain_section(
        d,
        DomainRole.JUDGE,
        modality="text_generation",
        benchmark_command=None,
        workspace_sources=(),
    )
    assert "./BENCHX" not in with_bench
    assert "./BENCHX" not in without_bench
    assert "audit the implementer's retained performance evidence" in with_bench
    assert "audit the implementer's retained performance evidence" in without_bench


def test_render_role_branches_on_interface(tmp_path: Path) -> None:
    """The process boundary reaches domain role files."""
    domain_dir = tmp_path / "domain"
    domain_dir.mkdir()
    (domain_dir / "judge.md").write_text(
        '{% if interface == "inprocess" %}IN_PROCESS_GATE{% endif %}\n'
    )
    d = _temporary_domain(domain_dir)
    inprocess = render_domain_section(d, DomainRole.JUDGE, interface="inprocess")
    service = render_domain_section(d, DomainRole.JUDGE, interface="service")
    assert "IN_PROCESS_GATE" in inprocess
    assert "IN_PROCESS_GATE" not in service


def test_single_agent_uses_explicit_section_when_present() -> None:
    # llm-serving ships a bespoke single_agent.md file
    d = resolve_domain(DomainName.LLM_SERVING)
    sa = render_domain_section(
        d,
        DomainRole.SINGLE_AGENT,
        modality="text_generation",
        reference_path="/ref",
        workspace_sources=(),
    )
    assert "do not let yourself cheat" in sa  # text unique to that section


def test_single_agent_derives_from_implementer_and_judge(tmp_path: Path) -> None:
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
        d,
        DomainRole.IMPLEMENTER,
        modality="text_generation",
        reference_path="/ref",
        workspace_sources=(),
    )
    criteria_text = "PC"
    return render_template(
        "implementer_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        modality="text_generation",
        interface="inprocess",
        domain_implementer=section,
        task="TASK",
        pass_criteria=criteria_text,
        reference_path="/ref",
        runtime_notes="",
        feedback=None,
        prior_attempt_artifact_locations=(),
        recommended_skills=[],
    )


def test_llm_serving_injects_into_implementer() -> None:
    out = _render_implementer(DomainName.LLM_SERVING)
    # serving-specific prose from the domain package is present
    assert "serving" in out.lower()
    assert "## Progress tracking" in out  # base skeleton intact


def test_generic_injects_nothing_extra() -> None:
    generic = _render_implementer(DomainName.GENERIC)
    # the only serving refs left are from the modality include, not the domain;
    # the generic render must be strictly shorter than llm-serving's.
    serving = _render_implementer(DomainName.LLM_SERVING)
    assert len(generic) < len(serving)
    assert "## Progress tracking" in generic  # base skeleton intact


def test_no_triple_blank_at_injection_point() -> None:
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
def test_orchestrator_is_a_domain_role() -> None:
    assert DomainRole.ORCHESTRATOR in DOMAIN_ROLES


def _render_orchestrator(domain: DomainName) -> str:
    section = render_domain_section(
        resolve_domain(domain),
        DomainRole.ORCHESTRATOR,
        modality="text_generation",
        workspace_sources=(),
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
        profile_execution="local",
    )


def test_llm_serving_provides_evidence_led_orchestrator_method() -> None:
    section = render_domain_section(
        resolve_domain(DomainName.LLM_SERVING),
        DomainRole.ORCHESTRATOR,
        modality="text_generation",
        workspace_sources=(),
    )
    assert "Evidence-led optimization method" in section
    assert "measured end-to-end evidence" in section
    assert "not technique popularity" in section
    assert "performance-modeling.md" in section
    assert "current-architecture ceiling" in section


def test_llm_serving_method_is_injected_into_plan() -> None:
    out = _render_orchestrator(DomainName.LLM_SERVING)
    assert "Evidence-led optimization method" in out
    assert "measured end-to-end evidence" in out


def test_generic_orchestrator_has_no_llm_serving_method() -> None:
    out = _render_orchestrator(DomainName.GENERIC)
    assert "Evidence-led optimization method" not in out
    assert "Continuous batching" not in out
    assert "the optimization-floor section below" not in out
    assert "## Task granularity" in out  # base skeleton intact


def test_database_provides_in_place_orchestrator_method() -> None:
    section = render_domain_section(
        resolve_domain(DomainName.DATABASE),
        DomainRole.ORCHESTRATOR,
        modality="dataflow",
        workspace_sources=(),
    )
    assert "In-place optimization planning guidance" in section
    # round-sized, one-change-per-round, correctness-gated micro-opt framing
    assert "micro-optimization" in section.lower()
    assert "one micro-optimization per round" in section.lower()
    assert "output-equivalence" in section.lower()


def test_database_method_is_injected_into_plan() -> None:
    out = _render_orchestrator(DomainName.DATABASE)
    assert "In-place optimization planning guidance" in out
    assert "## Task granularity" in out  # base skeleton intact


def test_llm_serving_profiler_branches_on_remote_execution_not_provider() -> None:
    domain = resolve_domain(DomainName.LLM_SERVING)

    local = render_domain_section(domain, DomainRole.PROFILER, profile_execution="local")
    remote = render_domain_section(domain, DomainRole.PROFILER, profile_execution="remote")

    assert "Run jobs for the same deployment" not in local
    assert "bounded remote controller/profile command" not in local
    assert "Run jobs for the same deployment" in remote
    assert "bounded controller/profile command" in remote
    assert "Modal" not in local
    assert "Modal" not in remote


def test_render_database_profiler_has_content() -> None:
    section = render_domain_section(
        resolve_domain(DomainName.DATABASE),
        DomainRole.PROFILER,
        profile_execution="local",
    )
    assert "Engine profile capture" in section
    # locate the hot path without displacing the scored metric
    assert "flamegraph" in section.lower()
    assert "headline cost metric" in section.lower()
    assert "output-equivalence" in section.lower()


def test_torch_profiler_remote_capture_is_provider_neutral() -> None:
    common = {
        "objective": "Measure service throughput.",
        "profile_focus": "Find the dominant accelerator bottleneck.",
        "runtime_notes": "Read the selected environment's runtime contract.",
        "benchmark_command": "./benchmark",
        "modality": "text_generation",
        "domain_profiler": "",
        "profiler_support_name": "torch_profiler",
        "profiler_mcp_name": "vibesys-torch-profiler",
    }

    local = render_template(
        "profilers/torch.j2",
        template_dir=_TEMPLATE_DIR,
        **common,
        profile_execution="local",
    )
    remote = render_template(
        "profilers/torch.j2",
        template_dir=_TEMPLATE_DIR,
        **common,
        profile_execution="remote",
    )

    assert "### Mode A: In-process" in local
    assert "### Remote capture (REQUIRED on this run)" not in local
    assert "### Remote capture (REQUIRED on this run)" in remote
    assert "representative workload must run on the remote candidate path" in remote
    assert "Suggestions are advisory" in local
    assert "Report visibility limits" in remote
    assert "Modal" not in local
    assert "Modal" not in remote
