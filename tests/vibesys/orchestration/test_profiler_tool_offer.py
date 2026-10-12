"""A profiler server offers a tool that needs a GPU driver only where the editor has one."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st
from resources.profilers._common import capture_runtime
from resources.profilers.ncu.server import build_server

from vibesys.orchestration.profilers import (
    ACTIVE_PROFILER_KINDS,
    PROFILER_DEFINITIONS,
    ProfilerKind,
)
from vibesys.run.profilers import WITHHELD_TOOLS_ENV, profiler_tool_offer

if TYPE_CHECKING:
    import pytest

_KINDS = st.sampled_from(sorted(ACTIVE_PROFILER_KINDS, key=str))
_NCU_DRIVER_TOOLS = {"query_metrics"}
_NCU_ALL_TOOLS = {"capabilities", "list_sections_and_sets", "query_metrics", "read_report"}


def _tool_names(withheld: frozenset[str]) -> set[str]:
    return {tool.name for tool in asyncio.run(build_server(withheld=withheld).list_tools())}


@given(kind=_KINDS, editor_has_gpu=st.booleans())
def test_offer_withholds_exactly_the_driver_tools_without_an_editor_gpu(
    kind: ProfilerKind, *, editor_has_gpu: bool
) -> None:
    definition = PROFILER_DEFINITIONS[kind]
    offer = profiler_tool_offer(kind, editor_has_gpu=editor_has_gpu)
    if editor_has_gpu:
        assert offer.withheld == ()
        assert offer.reason == ""
    else:
        assert set(offer.withheld) == definition.driver_tools
        assert bool(offer.reason) is bool(definition.driver_tools)
        for name in offer.withheld:
            assert name in offer.reason


@given(editor_has_gpu=st.booleans())
def test_ncu_server_lists_driver_tools_iff_the_editor_has_a_gpu(*, editor_has_gpu: bool) -> None:
    offer = profiler_tool_offer(ProfilerKind.NCU, editor_has_gpu=editor_has_gpu)
    names = _tool_names(frozenset(offer.withheld))
    assert names == (_NCU_ALL_TOOLS if editor_has_gpu else _NCU_ALL_TOOLS - _NCU_DRIVER_TOOLS)
    assert not (names & set(offer.withheld))


def test_ncu_declares_query_metrics_as_its_driver_tool() -> None:
    assert set(PROFILER_DEFINITIONS[ProfilerKind.NCU].driver_tools) == _NCU_DRIVER_TOOLS


def test_withheld_tools_env_is_the_name_the_server_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    assert capture_runtime.WITHHELD_TOOLS_ENV == WITHHELD_TOOLS_ENV
    monkeypatch.setenv(WITHHELD_TOOLS_ENV, "query_metrics, other,")
    assert capture_runtime.withheld_tools_from_env() == {"query_metrics", "other"}
    monkeypatch.delenv(WITHHELD_TOOLS_ENV)
    assert capture_runtime.withheld_tools_from_env() == frozenset()
