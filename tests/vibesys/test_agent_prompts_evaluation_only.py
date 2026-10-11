"""What the agent is told about trusted evaluation (#1649).

Every loop runs on the core path (dynamic, single, multi, plain/issue queue and evolve) and
gives its agents the evaluation tool, so their prompts describe evaluation only through that
tool: no ``vibesys-gate``, no ``--gate``, and ``vibesys-gpu`` only as ``-- COMMAND`` where the
agent's own GPU commands are on.
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

from vibesys.orchestration.dynamic.strategy.api import PromptTemplate
from vibesys.prompts import PROMPTS_DIR, render_template

if TYPE_CHECKING:
    from hypothesis.strategies import DataObject


_ENVIRONMENTS = PROMPTS_DIR / "environments"
# The limits the environment hands its notes template (``AgentGpuFacts`` in vs_runtime).
_GPU_FACTS = SimpleNamespace(max_gpus=4, max_time_minutes=90, read_only_paths=(), tool_paths=())
# What the evaluation-tool surface may say about vibesys-gpu: the command form, or the name.
_GPU_USE = re.compile(r"vibesys-gpu(`| --gpus N --time MINUTES -- COMMAND| -- COMMAND)")


def _notes(name: str) -> str:
    """The Slurm environment's notes, with the facts ``SlurmEnvironment.prepare`` reports."""
    gpu = name == "slurm+gpu"
    return render_template(
        "slurm/prompt_notes.j2",
        template_dir=_ENVIRONMENTS,
        service_command="python serve.py" if gpu else "",
        gpu_client="vibesys-gpu" if gpu else "",
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


@pytest.mark.parametrize("environment", ["slurm", "slurm+gpu"])
def test_the_dynamic_loops_environment_notes_describe_evaluation_only_through_the_tool(
    environment: str,
) -> None:
    _assert_evaluation_tool_only(_notes(environment), gpu=environment == "slurm+gpu")


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
        _plan_variables(tmp_path, _notes(environment)),
    )

    _assert_evaluation_tool_only(text, gpu=environment == "slurm+gpu")
    if template in (PromptTemplate.IMPLEMENT, PromptTemplate.REVIEW):
        assert "submit_evaluation" in text
        assert "validate_evaluation_wait" in text


def test_no_template_names_a_shell_gate() -> None:
    source_root = Path(__file__).resolve().parents[2] / "src" / "vibesys"

    for path in sorted(source_root.rglob("*.j2")):
        source = path.read_text(encoding="utf-8")
        assert "vibesys-gate" not in source, path
        assert "--gate" not in source, path
        assert "gate_client" not in source, path
