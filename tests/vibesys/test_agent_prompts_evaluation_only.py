"""What the agent is told about trusted evaluation depends on its loop (#1649).

The loops on the core path (dynamic, single and multi) give their agents the evaluation tool, so their prompts describe
evaluation only through that tool: no ``vibesys-gate``, no ``--gate``, and ``vibesys-gpu`` only as
``-- COMMAND`` where the agent's own GPU commands are on. Every older loop (plain/issue queue, evolve) has no evaluation tool and keeps the shell
gate until it is ported to the core path (roadmap #1952), so its notes are unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic.core_policy.test_environment_notes import (
    _FRESH_TURNS,
    _plan_variables,
    _render,
)
from tests.vibesys.orchestration.dynamic.core_policy.test_prompt_templates import _CONTEXTS

from vibesys.api import catalog
from vibesys.orchestration.dynamic.strategy.api import PromptTemplate
from vibesys.prompts import PROMPTS_DIR, render_template
from vibesys.run.host import offers_evaluation_tool

if TYPE_CHECKING:
    from hypothesis.strategies import DataObject

    from vibesys.plugin_registration import OrchestrationRegistration


_ENVIRONMENTS = PROMPTS_DIR / "environments"
# The limits the environment hands its notes template (``AgentGpuFacts`` in vs_runtime).
_GPU_FACTS = SimpleNamespace(max_gpus=4, max_time_minutes=90, read_only_paths=())
# What the evaluation-tool surface may say about vibesys-gpu: the command form, or the name.
_GPU_USE = re.compile(r"vibesys-gpu(`| --gpus N --time MINUTES -- COMMAND| -- COMMAND)")
_LOOPS = {
    "dynamic": catalog.DYNAMIC,
    "agent-multi": catalog.MULTI,
    "agent-single": catalog.SINGLE,
    "profile-guided-multi": catalog.PROFILE_GUIDED_MULTI,
    "profile-guided-single": catalog.PROFILE_GUIDED_SINGLE,
    "plain": catalog.ISSUE_QUEUE,
    "evolve": catalog.EVOLVE,
}
_GATES = ("accuracy", "benchmark")


def _notes(name: str, *, evaluation_tool: bool) -> str:
    """The Slurm environment's notes for a run whose loop does or does not have the tool.

    The facts are what ``SlurmEnvironment.prepare`` reports for the two modes.
    """
    gpu = name == "slurm+gpu"
    client = "vibesys-gpu" if gpu else "vibesys-gate"
    return render_template(
        "slurm/prompt_notes.j2",
        template_dir=_ENVIRONMENTS,
        service_command="python serve.py" if gpu else "",
        evaluation_tool=evaluation_tool,
        gate_client=client if gpu or not evaluation_tool else "",
        gates=() if evaluation_tool else _GATES,
        agent_gpu=_GPU_FACTS if gpu else None,
        read_only_paths=["/data"] if gpu else [],
    )


def _assert_evaluation_tool_only(text: str, *, gpu: bool) -> None:
    assert "vibesys-gate" not in text
    assert "--gate" not in text
    uses = [match.start() for match in re.finditer("vibesys-gpu", text)]
    assert len(uses) == len(_GPU_USE.findall(text))
    assert bool(uses) is gpu
    assert ("only the evaluation tool scores the candidate" in text) is gpu


@pytest.mark.parametrize("loop", _LOOPS)
def test_only_the_core_loops_have_the_evaluation_tool(loop: str) -> None:
    registration: OrchestrationRegistration = _LOOPS[loop]

    assert offers_evaluation_tool(registration.plugin) is (
        loop
        in {
            "dynamic",
            "agent-single",
            "profile-guided-single",
            "agent-multi",
            "profile-guided-multi",
        }
    )


@pytest.mark.parametrize("environment", ["slurm", "slurm+gpu"])
def test_the_dynamic_loops_environment_notes_describe_evaluation_only_through_the_tool(
    environment: str,
) -> None:
    _assert_evaluation_tool_only(
        _notes(environment, evaluation_tool=True), gpu=environment == "slurm+gpu"
    )


@pytest.mark.parametrize("environment", ["slurm", "slurm+gpu"])
def test_an_older_loops_environment_notes_still_name_the_gates_and_make_no_tool_claim(
    environment: str,
) -> None:
    text = _notes(environment, evaluation_tool=False)
    client = "vibesys-gpu" if environment == "slurm+gpu" else "vibesys-gate"

    for gate in _GATES:
        assert f"`{client} --gate {gate}`" in text
    assert "evaluation tool" not in text


@pytest.mark.parametrize("environment", ["slurm", "slurm+gpu"])
@pytest.mark.parametrize("template", _FRESH_TURNS, ids=lambda item: item.value)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=5)
@given(data=st.data())
def test_the_dynamic_loops_prompts_offer_the_evaluation_tool_only(
    environment: str, template: PromptTemplate, data: DataObject, tmp_path: Path
) -> None:
    text = _render(
        template,
        data.draw(_CONTEXTS[template]),
        _plan_variables(tmp_path, _notes(environment, evaluation_tool=True)),
    )

    _assert_evaluation_tool_only(text, gpu=environment == "slurm+gpu")
    if template in (PromptTemplate.IMPLEMENT, PromptTemplate.REVIEW):
        assert "submit_evaluation" in text
        assert "validate_evaluation_wait" in text


def test_the_dynamic_loops_own_templates_never_name_a_shell_gate() -> None:
    dynamic = Path(__file__).resolve().parents[2] / "src" / "vibesys" / "orchestration" / "dynamic"

    for path in sorted(dynamic.rglob("*.j2")):
        source = path.read_text(encoding="utf-8")
        assert "vibesys-gate" not in source, path
        assert "--gate" not in source, path
        assert "gate_client" not in source, path
