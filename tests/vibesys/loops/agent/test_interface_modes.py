"""Tests for topology-only in-process and service evaluation modes.

``--interface`` describes only how evaluator-owned code reaches the candidate:
direct invocation inside an evaluator process or communication with a service.
Domains, modalities, and input-owned contracts supply language, tooling, and
artifact requirements.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

import vibesys.domains.base as domain
from entrypoints.cli import parse_cli_invocation
from vibesys.api.contracts import LoopKind, RunRequest
from vibesys.config import as_config
from vibesys.constants import ComputeBackend, DomainName
from vibesys.domains.base import DomainRole
from vibesys.domains.registry import resolve_domain
from vibesys.domains.rendering import render_domain_section
from vibesys.errors import ConfigurationError
from vibesys.evaluators.input_manifest import load_input_bundle
from vibesys.loops.agent.loop import (
    _INTERFACES,
    DEFAULT_INTERFACE,
    _effective_profiler_definition,
    _profiler_prompt_template,
    run_agent_loop,
)
from vibesys.loops.metrics import MetricSpace
from vibesys.profilers import ProfilerKind
from vibesys.prompts import PROMPTS_DIR, render_template

if TYPE_CHECKING:
    from pathlib import Path
_TEMPLATE_DIR = PROMPTS_DIR / "loops" / "agent"
_CRITERION_TEXT = "PC"


def test_domain_module_has_no_language_axis() -> None:

    assert not hasattr(domain, "DEFAULT_LANGUAGE")
    assert not hasattr(domain, "LANGUAGE_DIR")
    assert not hasattr(domain, "DEFAULT_DOMAIN")


def test_no_language_pack_directory() -> None:
    assert not (_TEMPLATE_DIR / "_language").exists()


def _write_input_project(root: Path) -> Path:
    (root / "OBJECTIVE.md").write_text("Improve the queue.\n")
    (root / "vibesys.input.toml").write_text(
        'version = 1\n\n[agent]\ndomain = "generic"\n'
        '[accuracy]\ncommand = ["true"]\n'
        '[benchmark]\ncommand = ["true"]\n'
    )
    return root


@pytest.mark.parametrize("interface", ["inprocess", "service"])
def test_cli_exposes_only_process_boundary_modes(interface: str, tmp_path: Path) -> None:
    project = _write_input_project(tmp_path)
    invocation = parse_cli_invocation(["--input", str(project), "--interface", interface])
    assert invocation.args.interface == interface


def test_cli_default_interface_is_inprocess(tmp_path: Path) -> None:
    project = _write_input_project(tmp_path)
    invocation = parse_cli_invocation(["--input", str(project)])
    assert invocation.args.interface == "inprocess"


@pytest.mark.parametrize("interface", ["native", "rust"])
def test_cli_rejects_unknown_interface(interface: str, tmp_path: Path) -> None:
    project = _write_input_project(tmp_path)
    with pytest.raises(ConfigurationError, match="invalid choice"):
        parse_cli_invocation(["--input", str(project), "--interface", interface])


def test_loop_constants_and_rejects_unknown_interface(tmp_path: Path) -> None:

    assert DEFAULT_INTERFACE == "inprocess"
    assert _INTERFACES == ("inprocess", "service")
    project = _write_input_project(tmp_path)
    bundle = load_input_bundle(project)
    with pytest.raises(ValueError, match="interface"):
        run_agent_loop(
            RunRequest(
                project_root=project,
                loop=LoopKind.AGENT,
                config=as_config({"model": {"name": "test-model"}}),
                input_bundle=bundle,
                objective="o",
                exp_name="e",
                runs_dir=tmp_path,
                metrics=MetricSpace(),
                backend=ComputeBackend.CPU,
                interface="native",
            )
        )


def test_torch_profiler_honors_resolved_environment_capability() -> None:

    assert (
        _profiler_prompt_template(
            ProfilerKind.TORCH,
            supports_torch_profiler=True,
        )
        == "profilers/torch.j2"
    )
    with pytest.raises(ValueError, match="does not provide Torch profiler support"):
        _profiler_prompt_template(
            ProfilerKind.TORCH,
            supports_torch_profiler=False,
        )


def test_non_torch_profilers_use_the_resolved_kind() -> None:

    assert _profiler_prompt_template(ProfilerKind.NEURON) == "profilers/neuron.j2"
    assert _profiler_prompt_template(ProfilerKind.NSYS) == "profilers/nsys.j2"


def test_standalone_profiler_none_has_no_prompt_template() -> None:

    with pytest.raises(ValueError, match="disabled"):
        _profiler_prompt_template(ProfilerKind.NONE)


def test_standalone_profiler_rejects_unknown_kind() -> None:

    # The runtime guard is the subject here, so the declared type is violated
    # deliberately: a caller that skips static checking must still be rejected.
    with pytest.raises(TypeError, match="ProfilerKind"):
        _profiler_prompt_template(cast("ProfilerKind", "bogus"))


def _render_implementer(interface: str, *, modality: str | None = None) -> str:
    return render_template(
        "implementer_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        modality=modality,
        interface=interface,
        domain_implementer="",
        task="TASK",
        pass_criteria=_CRITERION_TEXT,
        reference_path="/ref",
        runtime_notes="",
        feedback=None,
        prior_attempt_artifact_locations=(),
        recommended_skills=[],
    )


def test_inprocess_prompt_describes_direct_invocation_without_language_assumptions() -> None:
    output = _render_implementer("inprocess")

    assert "invokes the candidate directly" in output
    assert "input-owned candidate contract" in output
    assert "Use `uv`" not in output
    assert "VibeServeModel" not in output
    assert "native artifact" not in output


def test_service_prompt_describes_network_boundary_without_language_assumptions() -> None:
    output = _render_implementer("service")

    assert "running candidate service" in output
    assert "network interface" in output
    assert "Use `uv`" not in output
    assert "VibeServeModel" not in output


def test_inprocess_implementer_handles_missing_reference_explicitly() -> None:
    output = render_template(
        "implementer_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        modality=None,
        interface="inprocess",
        domain_implementer="",
        task="TASK",
        pass_criteria=_CRITERION_TEXT,
        reference_path=".",
        runtime_notes="",
        feedback=None,
        prior_attempt_artifact_locations=(),
        recommended_skills=[],
    )
    assert "No separate reference implementation is provided" in output
    assert "Reference implementation is at `.`" not in output


def test_default_interface_matches_inprocess_for_implementer() -> None:

    explicit = _render_implementer("inprocess")
    implied = render_template(
        "implementer_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        modality=None,
        interface=DEFAULT_INTERFACE,
        domain_implementer="",
        task="TASK",
        pass_criteria=_CRITERION_TEXT,
        reference_path="/ref",
        runtime_notes="",
        feedback=None,
        prior_attempt_artifact_locations=(),
        recommended_skills=[],
    )
    assert explicit == implied


def test_llm_domain_owns_python_tooling() -> None:
    llm_domain = resolve_domain(DomainName.LLM_SERVING)
    generic_domain = resolve_domain(DomainName.GENERIC)

    llm_prompt = render_domain_section(
        llm_domain,
        DomainRole.IMPLEMENTER,
        interface="inprocess",
        workspace_sources=(),
    )
    generic_prompt = render_domain_section(
        generic_domain,
        DomainRole.IMPLEMENTER,
        interface="inprocess",
        workspace_sources=(),
    )
    assert "For candidate components that use Python, use `uv`" in llm_prompt
    assert "not a requirement that the serving hot path" in llm_prompt
    assert generic_prompt == ""


def _render_judge(interface: str) -> str:
    return render_template(
        "judge_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        modality="text_generation",
        interface=interface,
        domain_judge="",
        accuracy_command="accuracy-checker",
        benchmark_command="benchmark",
        pass_criteria=_CRITERION_TEXT,
        retry=1,
        runtime_notes="",
        profile_execution="local",
        objective="OBJ",
        pareto_archive_conflict=None,
    )


def test_text_generation_use_case_owns_inprocess_python_contract() -> None:
    assert "VibeServeModel" in _render_judge("inprocess")


def test_service_judge_drops_direct_import_contract() -> None:
    output = _render_judge("service")
    assert "VibeServeModel" not in output
    assert "Decode invariants" in output


def _render_single_agent(
    interface: str,
    *,
    profiler_kind: ProfilerKind = ProfilerKind.TORCH,
    supports_torch_profiler: bool = False,
) -> str:
    effective_profiler = (
        _effective_profiler_definition(
            profiler_kind,
            supports_torch_profiler=supports_torch_profiler,
        )
        if profiler_kind is not ProfilerKind.NONE
        else None
    )
    return render_template(
        "single_agent_round_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        modality=None,
        interface=interface,
        profile_execution="local",
        domain_single_agent="",
        domain_profiler="",
        task="TASK",
        pass_criteria=_CRITERION_TEXT,
        retry=1,
        feedback=None,
        objective="OBJ",
        profile_focus="focus",
        profiler_kind=profiler_kind,
        profiler_support_name=(effective_profiler.support_name if effective_profiler else None),
        profiler_mcp_name=(effective_profiler.mcp_name if effective_profiler else None),
        supports_torch_profiler=supports_torch_profiler,
        benchmark_command="benchmark",
        accuracy_command="accuracy-checker",
        reference_path="/ref",
        runtime_notes="",
    )


def test_inprocess_single_agent_uses_torch_only_for_supporting_domain() -> None:
    supported = _render_single_agent(
        "inprocess",
        supports_torch_profiler=True,
    )
    assert "torch.profiler" in supported
    with pytest.raises(ValueError, match="does not provide Torch profiler support"):
        _render_single_agent(
            "inprocess",
            supports_torch_profiler=False,
        )


def test_service_single_agent_honors_environment_resolved_torch() -> None:
    output = _render_single_agent(
        "service",
        supports_torch_profiler=True,
    )
    assert "torch.profiler" in output
    assert "nsys" not in output


def test_single_agent_profiler_none_avoids_profiler_tools() -> None:
    output = _render_single_agent("inprocess", profiler_kind=ProfilerKind.NONE)
    assert "Standalone profiling is disabled" in output
    assert "nsys_profiler" not in output
    assert "torch_profiler" not in output
    assert "neuron_profiler" not in output
