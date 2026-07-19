"""Tests for topology-only in-process and service evaluation modes.

``--interface`` describes only how evaluator-owned code reaches the candidate:
direct invocation inside an evaluator process or communication with a service.
Domains, modalities, and input-owned contracts supply language, tooling, and
artifact requirements.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vibesys.domains.base import DomainName, DomainRole
from vibesys.domains.registry import resolve_domain
from vibesys.domains.rendering import render_domain_section
from vibesys.errors import ConfigurationError
from vibesys.loops.agent.loop import _effective_profiler_definition
from vibesys.profilers import ProfilerKind
from vibesys.prompts import render_template

_TEMPLATE_DIR = (
    Path(__file__).resolve().parents[3] / "src" / "vibesys" / "loops" / "agent" / "templates"
)


def test_domain_module_has_no_language_axis():
    import vibesys.domains.base as domain

    assert not hasattr(domain, "DEFAULT_LANGUAGE")
    assert not hasattr(domain, "LANGUAGE_DIR")
    assert not hasattr(domain, "DEFAULT_DOMAIN")


def test_no_language_pack_directory():
    assert not (_TEMPLATE_DIR / "_language").exists()


def test_cli_exposes_only_process_boundary_modes():
    from vibesys.main import _build_agent_parser

    parser = _build_agent_parser()
    action = next(action for action in parser._actions if action.dest == "interface")
    assert set(action.choices) == {"inprocess", "service"}
    assert action.default == "inprocess"
    assert all(action.dest != "language" for action in parser._actions)


def test_cli_default_interface_is_inprocess():
    from vibesys.main import _build_agent_parser

    args = _build_agent_parser().parse_args(["--input", "/x", "--exp-name", "e"])
    assert args.interface == "inprocess"


@pytest.mark.parametrize("interface", ["native", "rust"])
def test_cli_rejects_unknown_interface(interface):
    from vibesys.main import _build_agent_parser

    with pytest.raises(ConfigurationError):
        _build_agent_parser().parse_args(
            ["--input", "/x", "--exp-name", "e", "--interface", interface]
        )


def test_loop_constants_and_rejects_unknown_interface():
    from vibesys.loops.agent.loop import (
        _INTERFACES,
        DEFAULT_INTERFACE,
        run_agent_loop,
    )

    assert DEFAULT_INTERFACE == "inprocess"
    assert _INTERFACES == ("inprocess", "service")
    with pytest.raises(ValueError, match="interface"):
        run_agent_loop(
            config=None,
            exp_name="e",
            input_path="/x",
            accuracy_command="accuracy-checker",
            benchmark_command="benchmark",
            objective="o",
            interface="native",
        )


def test_torch_profiler_requires_inprocess_boundary_and_domain_support():
    from vibesys.loops.agent.loop import _profiler_prompt_template

    assert (
        _profiler_prompt_template(
            ProfilerKind.TORCH,
            "inprocess",
            supports_torch_profiler=True,
        )
        == "profilers/torch.j2"
    )
    assert (
        _profiler_prompt_template(
            ProfilerKind.TORCH,
            "inprocess",
            supports_torch_profiler=False,
        )
        == "profilers/nsys.j2"
    )
    assert (
        _profiler_prompt_template(
            ProfilerKind.TORCH,
            "service",
            supports_torch_profiler=True,
        )
        == "profilers/nsys.j2"
    )


def test_non_torch_profilers_do_not_depend_on_interface():
    from vibesys.loops.agent.loop import _profiler_prompt_template

    assert _profiler_prompt_template(ProfilerKind.NEURON, "service") == "profilers/neuron.j2"
    assert _profiler_prompt_template(ProfilerKind.NSYS, "service") == "profilers/nsys.j2"


def test_standalone_profiler_none_has_no_prompt_template():
    from vibesys.loops.agent.loop import _profiler_prompt_template

    with pytest.raises(ValueError, match="disabled"):
        _profiler_prompt_template(ProfilerKind.NONE, "inprocess")


def test_standalone_profiler_rejects_unknown_kind():
    from vibesys.loops.agent.loop import _profiler_prompt_template

    with pytest.raises(TypeError, match="ProfilerKind"):
        _profiler_prompt_template("bogus", "inprocess")


def _render_implementer(interface: str, *, modality: str | None = None) -> str:
    return render_template(
        "implementer_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        modality=modality,
        interface=interface,
        domain_implementer="",
        task="TASK",
        pass_criteria="PC",
        reference_path="/ref",
        runtime_notes="",
        feedback=None,
    )


def test_inprocess_prompt_describes_direct_invocation_without_language_assumptions():
    output = _render_implementer("inprocess")

    assert "invokes the candidate directly" in output
    assert "input-owned candidate contract" in output
    assert "Use `uv`" not in output
    assert "VibeServeModel" not in output
    assert "native artifact" not in output


def test_service_prompt_describes_network_boundary_without_language_assumptions():
    output = _render_implementer("service")

    assert "running candidate service" in output
    assert "network interface" in output
    assert "Use `uv`" not in output
    assert "VibeServeModel" not in output


def test_inprocess_implementer_handles_missing_reference_explicitly():
    output = render_template(
        "implementer_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        modality=None,
        interface="inprocess",
        domain_implementer="",
        task="TASK",
        pass_criteria="PC",
        reference_path=".",
        runtime_notes="",
        feedback=None,
    )
    assert "No separate reference implementation is provided" in output
    assert "Reference implementation is at `.`" not in output


def test_default_interface_matches_inprocess_for_implementer():
    explicit = _render_implementer("inprocess")
    implied = render_template(
        "implementer_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        modality=None,
        domain_implementer="",
        task="TASK",
        pass_criteria="PC",
        reference_path="/ref",
        runtime_notes="",
        feedback=None,
    )
    assert explicit == implied


def test_llm_domain_owns_python_tooling():
    llm_domain = resolve_domain(DomainName.LLM_SERVING)
    generic_domain = resolve_domain(DomainName.GENERIC)

    llm_prompt = render_domain_section(
        llm_domain,
        DomainRole.IMPLEMENTER,
        interface="inprocess",
    )
    generic_prompt = render_domain_section(
        generic_domain,
        DomainRole.IMPLEMENTER,
        interface="inprocess",
    )
    assert "Use `uv` for Python package management" in llm_prompt
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
        pass_criteria="PC",
        retry=1,
        runtime_notes="",
        env_kind="local",
        objective="OBJ",
    )


def test_text_generation_use_case_owns_inprocess_python_contract():
    assert "VibeServeModel" in _render_judge("inprocess")


def test_service_judge_drops_direct_import_contract():
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
            interface,
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
        env_kind="local",
        domain_single_agent="",
        domain_profiler="",
        task="TASK",
        pass_criteria="PC",
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


def test_inprocess_single_agent_uses_torch_only_for_supporting_domain():
    supported = _render_single_agent(
        "inprocess",
        supports_torch_profiler=True,
    )
    unsupported = _render_single_agent(
        "inprocess",
        supports_torch_profiler=False,
    )
    assert "torch.profiler" in supported
    assert "torch.profiler" not in unsupported
    assert "nsys" in unsupported


def test_service_single_agent_avoids_inprocess_torch():
    output = _render_single_agent(
        "service",
        supports_torch_profiler=True,
    )
    assert "torch.profiler" not in output
    assert "nsys" in output


def test_single_agent_profiler_none_avoids_profiler_tools():
    output = _render_single_agent("inprocess", profiler_kind=ProfilerKind.NONE)
    assert "Standalone profiling is disabled" in output
    assert "nsys_profiler" not in output
    assert "torch_profiler" not in output
    assert "neuron_profiler" not in output
