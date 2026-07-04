"""Tests for nsys profiler integration: response models and agent-runner plumbing.

The orchestrate loop's profiler-gating behavior is covered in
``tests/test_orchestrate.py``; this module keeps the lower-level
ProfilerResponse / parser / nsys-toolkit tests.
"""

import json
import sqlite3
from unittest.mock import MagicMock

import pytest

from vibe_serve.agent_runner import (
    _parse_profiler_response_text,
    run_profiler_agent,
)
from vibe_serve.schemas import (
    ProfilerResponse,
)

# ---------------------------------------------------------------------------
# ProfilerResponse model tests
# ---------------------------------------------------------------------------


def test_profiler_response_creation():
    resp = ProfilerResponse(
        analysis="GPU is 85% busy, attention kernels dominate.",
        bottlenecks="1. flash_fwd_kernel (45% GPU time)\n2. rmsnorm_kernel (8%, 60 launches)",
        suggestions="Fuse RMSNorm kernels using FlashInfer ops.",
    )
    assert resp.analysis
    assert "flash_fwd_kernel" in resp.bottlenecks
    assert "FlashInfer" in resp.suggestions


def test_profiler_response_from_dict():
    data = {
        "analysis": "CPU launch overhead exceeds GPU exec time.",
        "bottlenecks": "Launch-bound: CPU/GPU ratio 1.7x",
        "suggestions": "Enable CUDA graphs for decode step.",
    }
    resp = ProfilerResponse.model_validate(data)
    assert resp.analysis == data["analysis"]


def test_profiler_response_serialization():
    resp = ProfilerResponse(
        analysis="Analysis.",
        bottlenecks="Bottlenecks.",
        suggestions="Suggestions.",
    )
    dumped = resp.model_dump()
    assert dumped["analysis"] == "Analysis."
    restored = ProfilerResponse.model_validate(dumped)
    assert restored == resp


# ---------------------------------------------------------------------------
# Profiler response parsing tests
# ---------------------------------------------------------------------------


def _profiler_json(**overrides):
    data = {
        "analysis": "Kernel analysis here.",
        "bottlenecks": "Top bottleneck: attention at 45%.",
        "suggestions": "Use CUDA graphs.",
    }
    data.update(overrides)
    return json.dumps(data)


def test_parse_profiler_response_raw_json():
    text = _profiler_json()
    resp = _parse_profiler_response_text(text)
    assert resp is not None
    assert resp.analysis == "Kernel analysis here."


def test_parse_profiler_response_fenced_json():
    text = f"```json\n{_profiler_json()}\n```"
    resp = _parse_profiler_response_text(text)
    assert resp is not None
    assert "attention" in resp.bottlenecks


def test_parse_profiler_response_with_surrounding_text():
    text = f"Here is the analysis:\n{_profiler_json()}\nDone."
    resp = _parse_profiler_response_text(text)
    assert resp is not None


def test_parse_profiler_response_empty():
    assert _parse_profiler_response_text("") is None
    assert _parse_profiler_response_text("no json here") is None


def test_parse_profiler_response_invalid_json():
    assert _parse_profiler_response_text("{invalid json}") is None


# ---------------------------------------------------------------------------
# run_profiler_agent tests
# ---------------------------------------------------------------------------


def test_run_profiler_agent_structured_response():
    """Agent returns structured response via stream."""
    agent = MagicMock()
    resp_data = ProfilerResponse(
        analysis="Good profile data.",
        bottlenecks="Attention dominates.",
        suggestions="No action needed.",
    )
    agent.stream.return_value = iter(
        [
            {
                "agent": {
                    "messages": [MagicMock(content="Profiled.", type="ai")],
                    "structured_response": resp_data,
                }
            }
        ]
    )
    result = run_profiler_agent(agent, "Profile the server.")
    assert result.analysis == "Good profile data."
    assert result.bottlenecks == "Attention dominates."


def test_run_profiler_agent_fallback_json_parsing():
    """Agent returns JSON text instead of structured response."""
    agent = MagicMock()
    json_text = _profiler_json(analysis="Fallback parsing.")
    agent.stream.return_value = iter(
        [
            {
                "agent": {
                    "messages": [MagicMock(content=json_text, type="ai")],
                }
            }
        ]
    )
    result = run_profiler_agent(agent, "Profile the server.")
    assert result.analysis == "Fallback parsing."


def test_run_profiler_agent_no_response():
    """Agent returns no parseable response."""
    agent = MagicMock()
    agent.stream.return_value = iter(
        [
            {
                "agent": {
                    "messages": [MagicMock(content="I couldn't profile.", type="ai")],
                }
            }
        ]
    )
    result = run_profiler_agent(agent, "Profile the server.")
    assert "No structured response" in result.analysis


# ---------------------------------------------------------------------------
# analyze_nsys.py tests (unit tests for analysis functions)
# ---------------------------------------------------------------------------


@pytest.fixture()
def nsys_db(tmp_path):
    """Create a minimal nsys-like SQLite database for testing."""
    db_path = tmp_path / "test.sqlite"
    conn = sqlite3.connect(str(db_path))

    # StringIds table
    conn.execute("CREATE TABLE StringIds (id INTEGER PRIMARY KEY, value TEXT)")
    conn.executemany(
        "INSERT INTO StringIds VALUES (?, ?)",
        [(1, "flash_fwd_kernel"), (2, "rmsnorm_kernel"), (3, "silu_and_mul")],
    )

    # CUPTI_ACTIVITY_KIND_KERNEL
    conn.execute("""
        CREATE TABLE CUPTI_ACTIVITY_KIND_KERNEL (
            shortName INTEGER, start INTEGER, end INTEGER,
            deviceId INTEGER, streamId INTEGER, correlationId INTEGER
        )
    """)
    # 3 kernels: flash_fwd (10us), rmsnorm (2us), silu (1us), with gaps
    kernels = [
        (1, 1000, 11000, 0, 7, 100),  # flash_fwd: 10us
        (2, 15000, 17000, 0, 7, 101),  # rmsnorm: 2us, gap=4us after flash
        (3, 20000, 21000, 0, 7, 102),  # silu: 1us, gap=3us after rmsnorm
    ]
    conn.executemany(
        "INSERT INTO CUPTI_ACTIVITY_KIND_KERNEL VALUES (?, ?, ?, ?, ?, ?)",
        kernels,
    )

    # CUPTI_ACTIVITY_KIND_RUNTIME
    conn.execute("""
        CREATE TABLE CUPTI_ACTIVITY_KIND_RUNTIME (
            start INTEGER, end INTEGER, cbid INTEGER, correlationId INTEGER,
            processId INTEGER, threadId INTEGER
        )
    """)
    runtime_calls = [
        (500, 1200, 33, 100, 1, 1),  # cudaLaunchKernel for flash_fwd
        (14500, 15100, 33, 101, 1, 1),  # cudaLaunchKernel for rmsnorm
        (19500, 20100, 33, 102, 1, 1),  # cudaLaunchKernel for silu
        (25000, 25500, 163, 200, 1, 1),  # cudaDeviceSynchronize
    ]
    conn.executemany(
        "INSERT INTO CUPTI_ACTIVITY_KIND_RUNTIME VALUES (?, ?, ?, ?, ?, ?)",
        runtime_calls,
    )

    # CUPTI_ACTIVITY_KIND_MEMCPY
    conn.execute("""
        CREATE TABLE CUPTI_ACTIVITY_KIND_MEMCPY (
            start INTEGER, end INTEGER, copyKind INTEGER, bytes INTEGER
        )
    """)
    conn.execute(
        "INSERT INTO CUPTI_ACTIVITY_KIND_MEMCPY VALUES (?, ?, ?, ?)",
        (100, 500, 1, 4096),  # HtoD copy
    )

    conn.commit()
    conn.close()
    return str(db_path)


def test_analyze_kernels(nsys_db):
    from examples.support.nsys_profiler.analyze_nsys import _build_string_map, analyze_kernels

    conn = sqlite3.connect(nsys_db)
    strings = _build_string_map(conn)
    result = analyze_kernels(conn, strings)
    conn.close()

    assert "flash_fwd_kernel" in result
    assert "rmsnorm_kernel" in result
    assert "silu_and_mul" in result
    assert "Total GPU kernel time" in result


def test_analyze_cpu_overhead(nsys_db):
    from examples.support.nsys_profiler.analyze_nsys import _build_string_map, analyze_cpu_overhead

    conn = sqlite3.connect(nsys_db)
    strings = _build_string_map(conn)
    result = analyze_cpu_overhead(conn, strings)
    conn.close()

    assert "CUDA runtime API calls" in result
    assert "cudaLaunchKernel" in result
    assert "Synchronization stalls" in result
    assert "cudaDeviceSynchronize" in result or "1 calls" in result


def test_analyze_gpu_idle_gaps(nsys_db):
    from examples.support.nsys_profiler.analyze_nsys import _build_string_map, analyze_gpu_idle_gaps

    conn = sqlite3.connect(nsys_db)
    strings = _build_string_map(conn)
    result = analyze_gpu_idle_gaps(conn, strings)
    conn.close()

    assert "GPU busy" in result
    assert "GPU idle" in result
    assert "idle gaps" in result.lower() or "Idle gaps" in result


def test_analyze_memory_ops(nsys_db):
    from examples.support.nsys_profiler.analyze_nsys import analyze_memory_ops

    conn = sqlite3.connect(nsys_db)
    result = analyze_memory_ops(conn)
    conn.close()

    assert "HtoD" in result


def test_short_kernel_name():
    from examples.support.nsys_profiler.analyze_nsys import _short_kernel_name

    assert (
        _short_kernel_name("void at::native::vectorized_elementwise_kernel<4, float>")
        == "native::vectorized_elementwise_kernel"
    )
    assert _short_kernel_name("simple_kernel") == "simple_kernel"
    assert _short_kernel_name("a::b::c::func") == "c::func"
