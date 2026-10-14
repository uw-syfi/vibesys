"""A profiler prompt never prints an absent benchmark command as the word None."""

from __future__ import annotations

from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.reply_variables import profiler_reply

import vibesys.orchestration.multi.core_policy.prompts as multi_templates
from vibesys.prompts import render_template
from vs_sim.api.testing import examples

_TEMPLATE_DIR = Path(multi_templates.__file__).parent
_KINDS = ("linux_cpu", "macos_cpu", "otel")


def _render(kind: str, benchmark_command: str | None) -> str:
    return render_template(
        f"profilers/{kind}.j2",
        template_dir=_TEMPLATE_DIR,
        reply=profiler_reply(),
        profile_focus="",
        benchmark_command=benchmark_command,
        modality="",
        domain_profiler="",
        runtime_notes="",
        objective="OBJ",
        profiler_support_name=f"{kind}_profiler",
        profiler_mcp_name=f"vibesys-{kind.replace('_', '-')}-profiler",
        campaign=None,
    )


@pytest.mark.parametrize("kind", _KINDS)
@pytest.mark.parametrize("absent", [None, ""])
def test_an_absent_benchmark_command_points_at_the_objective(kind: str, absent: str | None) -> None:
    output = _render(kind, absent)
    assert "None" not in output
    assert "OBJECTIVE.md" in output


@settings(max_examples=examples(pr=8, nightly=100))
@given(command=st.text(alphabet="abcdefgh -./", min_size=1, max_size=20).filter(str.strip))
@pytest.mark.parametrize("kind", _KINDS)
def test_a_present_benchmark_command_is_named(kind: str, command: str) -> None:
    assert command in _render(kind, command)
