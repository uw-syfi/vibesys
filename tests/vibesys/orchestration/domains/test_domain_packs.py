"""Tests for registered domains, the manifest-selected pluggable context.

Covers the resolver (registered name / error), the role-file renderer (present,
empty, missing, ``single_agent`` derivation, context branching) and the profiler templates the
sections are passed to. `test_domain_sections_in_prompts.py` covers the strategy prompts.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest
from tests.vibesys.reply_variables import profiler_reply

import vibesys.orchestration.multi.core_policy.prompts as multi_templates
from vibesys.constants import DomainName
from vibesys.domains.base import DOMAIN_ROLES, DomainDefinition, DomainRole
from vibesys.domains.registry import (
    DOMAINS,
    resolve_domain,
)
from vibesys.domains.rendering import render_domain_section as _render_domain_section
from vibesys.prompts import render_template

_TEMPLATE_DIR = Path(multi_templates.__file__).parent
# The variable set `domain_prompt_sections` always passes; a test overrides what it varies.
_RUN_CONTEXT: dict[str, object] = {
    "modality": None,
    "interface": "inprocess",
    "reference_path": ".",
    "benchmark_command": None,
    "accuracy_command": None,
    "runtime_notes": "",
    "profile_execution": "local",
    "workspace_sources": (),
}


def render_domain_section(domain: DomainDefinition, role: DomainRole, **context: object) -> str:
    return _render_domain_section(domain, role, **(_RUN_CONTEXT | context))


def _temporary_domain(prompt_dir: Path) -> DomainDefinition:
    return DomainDefinition(
        name=DomainName.GENERIC,
        prompt_dir=prompt_dir,
    )


# --------------------------------------------------------------------------- #
# resolver
# --------------------------------------------------------------------------- #
def test_registered_domains_present() -> None:
    names = sorted(domain.value for domain in DOMAINS)
    assert "llm-serving" in names
    assert "generic" in names
    assert "microservices" in names
    assert "database" in names
    assert "kernel-writing" in names
    assert "README" not in names  # the authoring guide is not a domain


def test_resolve_registered_name() -> None:
    d = resolve_domain(DomainName.LLM_SERVING)
    assert d.name is DomainName.LLM_SERVING
    assert d.prompt_dir.is_dir()
    assert d.prompt_dir.name == "prompts"
    assert d.prompt_dir.parent.name == "llm_serving"


def test_resolve_microservices_domain() -> None:
    d = resolve_domain(DomainName.MICROSERVICES)
    assert d.name is DomainName.MICROSERVICES
    assert d.prompt_dir.is_dir()
    assert d.prompt_dir.name == "prompts"
    assert d.prompt_dir.parent.name == "microservices"


def test_resolve_database_domain() -> None:
    d = resolve_domain(DomainName.DATABASE)
    assert d.name is DomainName.DATABASE
    assert d.prompt_dir.is_dir()
    assert d.prompt_dir.name == "prompts"
    assert d.prompt_dir.parent.name == "database"


def test_kernel_writing_domain_renders_roles_and_derives_single_agent() -> None:
    domain = resolve_domain(DomainName.KERNEL_WRITING)
    implementer = render_domain_section(domain, DomainRole.IMPLEMENTER)
    judge = render_domain_section(domain, DomainRole.JUDGE)
    profiler = render_domain_section(domain, DomainRole.PROFILER)
    single_agent = render_domain_section(domain, DomainRole.SINGLE_AGENT)

    assert domain.prompt_dir.is_dir()
    assert implementer
    assert judge
    assert profiler
    assert single_agent == f"{implementer}\n\n{judge}"


def test_resolve_path_is_not_supported(tmp_path: Path) -> None:
    f = tmp_path / "mine"
    f.mkdir()
    (f / "implementer.md").write_text("hello\n")
    # The runtime guard is the subject here, so the declared type is violated
    # deliberately: a path string must not resolve as a domain.
    with pytest.raises(TypeError, match="DomainName"):
        resolve_domain(cast("DomainName", str(f)))


def test_domains_declare_torch_profiler_compatibility() -> None:
    assert DOMAINS[DomainName.LLM_SERVING].supports_torch_profiler
    assert not DOMAINS[DomainName.GENERIC].supports_torch_profiler
    assert not DOMAINS[DomainName.MICROSERVICES].supports_torch_profiler
    assert not DOMAINS[DomainName.DATABASE].supports_torch_profiler
    assert not DOMAINS[DomainName.KERNEL_WRITING].supports_torch_profiler


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
        profiler_id="none",
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
# --------------------------------------------------------------------------- #
# orchestrator role
# --------------------------------------------------------------------------- #
def test_orchestrator_is_a_domain_role() -> None:
    assert DomainRole.ORCHESTRATOR in DOMAIN_ROLES


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


def test_llm_serving_profiler_branches_on_remote_execution_not_provider() -> None:
    domain = resolve_domain(DomainName.LLM_SERVING)

    local = render_domain_section(
        domain, DomainRole.PROFILER, profile_execution="local", profiler_id="none"
    )
    remote = render_domain_section(
        domain, DomainRole.PROFILER, profile_execution="remote", profiler_id="none"
    )

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
        profiler_id="none",
    )
    assert "Engine profile capture" in section
    # locate the hot path without displacing the scored metric
    assert "flamegraph" in section.lower()
    assert "headline cost metric" in section.lower()
    assert "output-equivalence" in section.lower()


def _serving_profiler(profile_execution: str) -> str:
    """The llm-serving pack's profiler section, which the profiler prompt embeds."""
    return render_domain_section(
        resolve_domain(DomainName.LLM_SERVING),
        DomainRole.PROFILER,
        modality="text_generation",
        profile_execution=profile_execution,
        profiler_id="none",
    )


def test_torch_profiler_remote_capture_is_provider_neutral() -> None:
    common = {
        "objective": "Measure service throughput.",
        "profile_focus": "Find the dominant accelerator bottleneck.",
        "runtime_notes": "Read the selected environment's runtime contract.",
        "benchmark_command": "./benchmark",
        "profiler_support_name": "torch_profiler",
        "profiler_mcp_name": "vibesys-torch-profiler",
        "campaign": None,
    }

    local = render_template(
        "profilers/torch.j2",
        template_dir=_TEMPLATE_DIR,
        reply=profiler_reply(),
        **common,
        domain_profiler=_serving_profiler("local"),
        profile_execution="local",
        profiler_id="none",
    )
    remote = render_template(
        "profilers/torch.j2",
        template_dir=_TEMPLATE_DIR,
        reply=profiler_reply(),
        **common,
        domain_profiler=_serving_profiler("remote"),
        profile_execution="remote",
        profiler_id="none",
    )

    assert "`profile_ops`: generic in-process capture" in local
    assert "### Remote capture (REQUIRED on this run)" not in local
    assert "### Remote capture (REQUIRED on this run)" in remote
    assert "representative workload must run on the remote candidate path" in remote
    assert "Suggestions are advisory" in local
    assert "Report visibility limits" in remote
    assert "Modal" not in local
    assert "Modal" not in remote


@pytest.mark.parametrize("role", [DomainRole.IMPLEMENTER, DomainRole.PROFILER])
@pytest.mark.parametrize("profiler_id", ["nsys", "rocprof", "torch", "linux_cpu", "none"])
def test_llm_serving_names_nvtx_only_for_the_nsys_profiler(
    role: DomainRole, profiler_id: str
) -> None:
    section = render_domain_section(
        resolve_domain(DomainName.LLM_SERVING),
        role,
        modality="text_generation",
        interface="service",
        profile_execution="local",
        profiler_id=profiler_id,
        reference_path=".",
        workspace_sources=(),
    )

    assert ("NVTX" in section) is (profiler_id == "nsys")
    assert "nsys" not in section or profiler_id == "nsys"


@pytest.mark.parametrize("interface", ["inprocess", "service"])
def test_llm_serving_names_the_import_adapter_only_for_an_inprocess_candidate(
    interface: str,
) -> None:
    section = render_domain_section(
        resolve_domain(DomainName.LLM_SERVING),
        DomainRole.IMPLEMENTER,
        modality="text_generation",
        interface=interface,
        profile_execution="local",
        profiler_id="none",
        reference_path=".",
        workspace_sources=(),
    )

    assert ("VibeServeModel" in section) is (interface == "inprocess")
