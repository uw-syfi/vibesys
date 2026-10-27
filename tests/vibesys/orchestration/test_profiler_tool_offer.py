"""A profiler server offers a tool that needs a GPU driver only where the editor has one."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from hypothesis import given, settings
from hypothesis import strategies as st
from resources.profilers._common import capture_runtime
from resources.profilers.ncu.server import build_server
from resources.profilers.ncu_flashinfer.server import TIMELINE_PREFIX
from resources.profilers.ncu_flashinfer.server import build_server as build_ncu_flashinfer
from resources.profilers.rocprof.server import build_server as build_rocprof

from vibesys.orchestration.profilers import (
    ACTIVE_PROFILER_KINDS,
    PROFILER_DEFINITIONS,
    ProfilerKind,
)
from vibesys.run.profilers import WITHHELD_TOOLS_ENV, profiler_tool_offer
from vs_sim.api.testing import examples

if TYPE_CHECKING:
    import pytest

_KINDS = st.sampled_from(sorted(ACTIVE_PROFILER_KINDS, key=str))
_NCU_DRIVER_TOOLS = {"query_metrics"}
_NCU_ALL_TOOLS = {"capabilities", "list_sections_and_sets", "query_metrics", "read_report"}
_TIMELINE_TOOLS = {
    TIMELINE_PREFIX + name
    for name in (
        "capabilities",
        "summary",
        "tracks",
        "event_skew",
        "track_window",
        "export_chrome_trace",
    )
}


def _tool_names(withheld: frozenset[str]) -> set[str]:
    return {tool.name for tool in asyncio.run(build_server(withheld=withheld).list_tools())}


@settings(max_examples=examples(pr=8, nightly=100))
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


@settings(max_examples=examples(pr=8, nightly=100))
@given(editor_has_gpu=st.booleans())
def test_ncu_server_lists_driver_tools_iff_the_editor_has_a_gpu(*, editor_has_gpu: bool) -> None:
    offer = profiler_tool_offer(ProfilerKind.NCU, editor_has_gpu=editor_has_gpu)
    names = _tool_names(frozenset(offer.withheld))
    assert names == (_NCU_ALL_TOOLS if editor_has_gpu else _NCU_ALL_TOOLS - _NCU_DRIVER_TOOLS)
    assert not (names & set(offer.withheld))


@settings(max_examples=examples(pr=4, nightly=50))
@given(editor_has_gpu=st.booleans())
def test_ncu_flashinfer_server_offers_both_parts_minus_withheld(*, editor_has_gpu: bool) -> None:
    offer = profiler_tool_offer(ProfilerKind.NCU_FLASHINFER, editor_has_gpu=editor_has_gpu)
    server = build_ncu_flashinfer(withheld=frozenset(offer.withheld))
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    ncu = _NCU_ALL_TOOLS if editor_has_gpu else _NCU_ALL_TOOLS - _NCU_DRIVER_TOOLS
    assert names == ncu | _TIMELINE_TOOLS


def test_ncu_declares_query_metrics_as_its_driver_tool() -> None:
    assert set(PROFILER_DEFINITIONS[ProfilerKind.NCU].driver_tools) == _NCU_DRIVER_TOOLS


def test_withheld_tools_env_is_the_name_the_server_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    assert capture_runtime.WITHHELD_TOOLS_ENV == WITHHELD_TOOLS_ENV
    monkeypatch.setenv(WITHHELD_TOOLS_ENV, "query_metrics, other,")
    assert capture_runtime.withheld_tools_from_env() == {"query_metrics", "other"}
    monkeypatch.delenv(WITHHELD_TOOLS_ENV)
    assert capture_runtime.withheld_tools_from_env() == frozenset()


@settings(max_examples=examples(pr=8, nightly=100))
@given(editor_has_gpu=st.booleans(), captures_remotely=st.booleans())
def test_rocprof_capture_tools_are_offered_iff_the_editor_has_a_gpu_or_capture_is_remote(
    *, editor_has_gpu: bool, captures_remotely: bool
) -> None:
    offer = profiler_tool_offer(
        ProfilerKind.ROCPROF, editor_has_gpu=editor_has_gpu, captures_remotely=captures_remotely
    )
    declared = PROFILER_DEFINITIONS[ProfilerKind.ROCPROF].driver_tools
    names = _rocprof_tool_names(frozenset(offer.withheld))
    if editor_has_gpu or captures_remotely:
        assert offer.withheld == ()
        assert declared <= names
    else:
        assert set(offer.withheld) == declared
        assert not (declared & names)
        # report-reading tools stay
        assert {"summary", "kernels", "compare", "query"} <= names


def test_rocprof_declares_every_gpu_launching_tool_the_server_registers() -> None:
    all_names = _rocprof_tool_names(frozenset())
    declared = PROFILER_DEFINITIONS[ProfilerKind.ROCPROF].driver_tools
    assert declared <= all_names
    launching = {n for n in all_names if n.startswith(("profile_", "submit_"))}
    assert launching <= declared


def _rocprof_tool_names(withheld: frozenset[str]) -> set[str]:
    return {tool.name for tool in asyncio.run(build_rocprof(withheld=withheld).list_tools())}
