"""Tests for ``--interface`` — the in-process-vs-service evaluation contract.

The implementation language is no longer a user-facing axis (there is no
``--language`` flag and no ``_language/`` packs). Instead the run's
``--interface`` mode fixes the contract by which the accuracy checker and
benchmark reach the artifact, and that contract decides the language:

- ``inprocess`` (default): the checker imports ``main.py`` in process, so the
  implementation is Python — the prompts carry the ``uv`` toolchain and the
  ``VibeServeModel`` import contract.
- ``service``: the artifact is exercised only over the wire, so the agent picks
  the language — the in-process contract and the Python/uv tooling drop out and a
  language-freedom block takes their place.

These tests pin that behaviour at the prompt layer plus the CLI/loop wiring.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vibe_serve.domains.base import DomainName
from vibe_serve.profilers import ProfilerKind
from vibe_serve.prompts import render_template

_TEMPLATE_DIR = (
    Path(__file__).resolve().parents[3] / "src" / "vibe_serve" / "loops" / "agent" / "templates"
)


# --------------------------------------------------------------------------- #
# no user-facing language axis
# --------------------------------------------------------------------------- #
def test_domain_module_has_no_language_axis():
    """The implementation language is carried by ``--interface``, not a pack.
    The domain module exposes only the domain axis — no language constants."""
    import vibe_serve.domains.base as domain

    assert not hasattr(domain, "DEFAULT_LANGUAGE")
    assert not hasattr(domain, "LANGUAGE_DIR")
    # the domain axis is untouched
    assert domain.DEFAULT_DOMAIN is DomainName.LLM_SERVING


def test_no_language_pack_directory():
    assert not (_TEMPLATE_DIR / "_language").exists()


# --------------------------------------------------------------------------- #
# CLI wiring
# --------------------------------------------------------------------------- #
def test_cli_exposes_interface_not_language():
    from vibe_serve.cli import _build_agent_parser

    parser = _build_agent_parser()
    # --interface is present with the two modes and defaults to inprocess
    action = next(a for a in parser._actions if a.dest == "interface")
    assert set(action.choices) == {"inprocess", "service"}
    assert action.default == "inprocess"
    # --language is gone
    assert all(a.dest != "language" for a in parser._actions)


def test_cli_default_interface_is_inprocess():
    from vibe_serve.cli import _build_agent_parser

    args = _build_agent_parser().parse_args(["--ref", "/x", "--exp-name", "e"])
    assert args.interface == "inprocess"


def test_cli_rejects_unknown_interface():
    from vibe_serve.cli import _build_agent_parser

    with pytest.raises(SystemExit):
        _build_agent_parser().parse_args(["--ref", "/x", "--exp-name", "e", "--interface", "rust"])


# --------------------------------------------------------------------------- #
# loop validation
# --------------------------------------------------------------------------- #
def test_loop_constants_and_rejects_unknown_interface():
    from vibe_serve.loops.agent.loop import (
        _INTERFACES,
        DEFAULT_INTERFACE,
        run_agent_loop,
    )

    assert DEFAULT_INTERFACE == "inprocess"
    assert _INTERFACES == ("inprocess", "service")
    # validation happens before any heavy setup, so dummy args are fine
    with pytest.raises(ValueError, match="interface"):
        run_agent_loop(
            config=None,
            exp_name="e",
            reference_path="/x",
            objective="o",
            interface="rust",
        )


# --------------------------------------------------------------------------- #
# standalone (multi-agent) profiler selection
# --------------------------------------------------------------------------- #
def test_standalone_profiler_drops_torch_under_service():
    """The multi-agent standalone profiler must treat the torch profiler the
    same way the single-agent prompt does: torch is a white-box, in-process
    PyTorch tool, so ``--interface service`` (exercised only over the wire, any
    language) falls back to the black-box nsys profiler."""
    from vibe_serve.loops.agent.loop import _profiler_prompt_template

    # inprocess keeps the white-box torch profiler
    assert _profiler_prompt_template(ProfilerKind.TORCH, "inprocess") == "profiler_prompt_torch.j2"
    # service swaps torch -> nsys (torch imports the implementation)
    assert _profiler_prompt_template(ProfilerKind.TORCH, "service") == "profiler_prompt_nsys.j2"
    # non-torch kinds are unaffected by the interface
    assert _profiler_prompt_template(ProfilerKind.NEURON, "service") == "profiler_prompt_neuron.j2"
    assert _profiler_prompt_template(ProfilerKind.NSYS, "service") == "profiler_prompt_nsys.j2"


def test_standalone_profiler_none_has_no_prompt_template():
    from vibe_serve.loops.agent.loop import _profiler_prompt_template

    with pytest.raises(ValueError, match="disabled"):
        _profiler_prompt_template(ProfilerKind.NONE, "inprocess")


def test_standalone_profiler_rejects_unknown_kind():
    from vibe_serve.loops.agent.loop import _profiler_prompt_template

    with pytest.raises(TypeError, match="ProfilerKind"):
        _profiler_prompt_template("bogus", "inprocess")


# --------------------------------------------------------------------------- #
# prompt rendering: implementer
# --------------------------------------------------------------------------- #
def _render_implementer(interface: str) -> str:
    return render_template(
        "implementer_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        modality="text_generation",
        interface=interface,
        domain_implementer="",  # isolate the interface axis
        task="TASK",
        pass_criteria="PC",
        reference_path="/ref",
        runtime_notes="",
        feedback=None,
    )


def test_inprocess_implementer_keeps_python_contract():
    out = _render_implementer("inprocess")
    # uv toolchain prose and the in-process import contract are both present
    assert "uv" in out
    assert "VibeServeModel" in out
    assert "from main import VibeServeModel" in out


def test_service_implementer_is_language_free():
    out = _render_implementer("service")
    # the in-process Python contract and uv tooling are gone
    assert "VibeServeModel" not in out
    assert "uv" not in out
    # ...replaced by an explicit language-freedom statement
    assert "over the wire" in out
    assert "language" in out.lower()


def test_default_interface_matches_inprocess_for_implementer():
    """Rendering with no interface set must equal explicit inprocess (the
    template's own default), so existing runs are unchanged."""
    explicit = _render_implementer("inprocess")
    implied = render_template(
        "implementer_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        modality="text_generation",
        domain_implementer="",
        task="TASK",
        pass_criteria="PC",
        reference_path="/ref",
        runtime_notes="",
        feedback=None,
    )
    assert explicit == implied


# --------------------------------------------------------------------------- #
# prompt rendering: judge
# --------------------------------------------------------------------------- #
def _render_judge(interface: str) -> str:
    return render_template(
        "judge_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        modality="text_generation",
        interface=interface,
        domain_judge="",
        accuracy_checker_path="/acc",
        bench_path="/bench",
        pass_criteria="PC",
        retry=1,
        runtime_notes="",
        env_kind="local",
        objective="OBJ",
    )


def test_inprocess_judge_requires_vibeservemodel():
    assert "VibeServeModel" in _render_judge("inprocess")


def test_service_judge_drops_vibeservemodel():
    out = _render_judge("service")
    assert "VibeServeModel" not in out
    # decode invariants (modality content) still render
    assert "Decode invariants" in out


# --------------------------------------------------------------------------- #
# prompt rendering: single-agent
# --------------------------------------------------------------------------- #
def _render_single_agent(interface: str, profiler_kind: ProfilerKind = ProfilerKind.TORCH) -> str:
    return render_template(
        "single_agent_round_prompt.j2",
        template_dir=_TEMPLATE_DIR,
        modality="text_generation",
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
        bench_path="/bench",
        accuracy_checker_path="/acc",
        reference_path="/ref",
        runtime_notes="",
    )


def test_inprocess_single_agent_keeps_uv_and_torch_capture():
    out = _render_single_agent("inprocess", profiler_kind=ProfilerKind.TORCH)
    assert "uv" in out
    # torch in-process capture path is offered for Python implementations
    assert "torch.profiler" in out


def test_service_single_agent_is_language_free_and_avoids_inprocess_torch():
    out = _render_single_agent("service", profiler_kind=ProfilerKind.TORCH)
    assert "uv" not in out
    assert "over the wire" in out
    # torch in-process capture is Python-only; service falls back to nsys /
    # server-driven profiling instead
    assert "torch.profiler" not in out
    assert "nsys" in out


def test_single_agent_profiler_none_avoids_profiler_tools():
    out = _render_single_agent("inprocess", profiler_kind=ProfilerKind.NONE)
    assert "Standalone profiling is disabled" in out
    assert "nsys_profiler" not in out
    assert "torch_profiler" not in out
    assert "neuron_profiler" not in out
    assert "vibeserve-nsys-profiler" not in out
    assert "vibeserve-torch-profiler" not in out
