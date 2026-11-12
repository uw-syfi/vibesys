"""AMD workload tasks and the TraceLab adapter's public result validator."""

from __future__ import annotations

import csv
import hashlib
import json
import runpy
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.examples.example_task import load_example_task, task_root

from vibesys_evaluator.llm_serving import _agentx

_PROJECT = Path(__file__).parents[2] / "examples/model-serving/qwen3.6-35b-a3b"
_TRACE = _PROJECT / ".vibesys/shared/tracelab"
_REPLAY = SimpleNamespace(**runpy.run_path(str(_TRACE / "replay.py")))


_HARDWARE = {"mi2104x": ("AMD MI210", 4), "mi300x": ("AMD MI300X", 1)}
_AGENTX_TASKS = [f"{hardware}-agentx" for hardware in _HARDWARE]
# The benchmark arguments of the schema-0.1 AgentX tasks, which ran the `agentx-v1` entrypoint of
# resources/evaluators/agentx; the schema-0.2 tasks must replay exactly this recipe.
_AGENTX_V1_ARGS = [
    "--mode", "full",
    "--model", "Qwen/Qwen3.6-35B-A3B",
    "--tokenizer", "Qwen/Qwen3.6-35B-A3B",
    "--tokenizer-revision", "995ad96eacd98c81ed38be0c5b274b04031597b0",
    "--concurrency", "4",
    "--max-context-length", "262144",
]  # fmt: skip


def _manifest(task: str) -> dict:
    return tomllib.loads((task_root(_PROJECT, task) / "vibesys.input.toml").read_text())


def test_tracelab_tasks_share_workload_but_declare_distinct_hardware() -> None:
    small = load_example_task(_PROJECT, "mi2104x-tracelab").manifest
    large = load_example_task(_PROJECT, "mi300x-tracelab").manifest
    assert small.catalogue is not None
    assert large.catalogue is not None
    assert (small.catalogue.hardware.accelerator, small.catalogue.hardware.count) == (
        _HARDWARE["mi2104x"]
    )
    assert (large.catalogue.hardware.accelerator, large.catalogue.hardware.count) == (
        _HARDWARE["mi300x"]
    )
    assert small.backends == large.backends
    assert small.benchmark == large.benchmark
    assert small.accuracy == large.accuracy
    assert small.benchmark.result_protocol == 2
    assert small.benchmark.entrypoint == "request-factory-adapter"


@pytest.mark.parametrize("workload", ["agentx", "tracelab"])
def test_amd_tasks_score_the_same_five_objectives(workload: str) -> None:
    for hardware in _HARDWARE:
        root = task_root(_PROJECT, f"{hardware}-{workload}")
        objectives = tomllib.loads((root / "objectives.toml").read_text())
        assert {item["name"] for item in objectives["objective"]} == set(_REPLAY.METRICS)
        assert objectives["pareto"]["relative_noise"] == 0.03
        assert _REPLAY.REVISION in (root / "reference/meta.json").read_text()


@pytest.mark.parametrize("task", _AGENTX_TASKS)
def test_agentx_tasks_are_two_image_tasks_of_the_baseline_engine(task: str) -> None:
    manifest = _manifest(task)
    assert manifest["version"] == "0.2"
    assert manifest["backends"] == ["rocm"]
    assert "evaluator" not in manifest
    # [profile] is not part of these tasks: the smoke replay is a command in the task image.
    assert "profile" not in manifest
    assert set(manifest["environment"]["needs"]) == {"rocm", "mounts"}
    candidate = manifest["candidate"]
    assert (candidate["dockerfile"], candidate["context"]) == ("Dockerfile.rocm", ".")
    assert (_PROJECT / candidate["dockerfile"]).is_file()
    assert candidate["serve"] == [
        "python", "-m", "engine.server", "--host", "127.0.0.1", "--port", "${PORT}",
    ]  # fmt: skip
    assert candidate["ready"] == "/health"
    assert candidate["startup_timeout_seconds"] == 1800
    assert manifest["accuracy"] == {
        "command": ["sh", "-c", 'exec python3 -I /task/checker.py --url "$VIBESYS_SERVICE_URL"'],
        "timeout_seconds": 300,
    }
    assert manifest["benchmark"] == {
        "command": ["python", "/task/bench.py", "benchmark"],
        "timeout_seconds": 3600,
        "result_protocol": 2,
    }
    hardware = manifest["catalogue"]["hardware"]
    assert (hardware["accelerator"], hardware["count"], hardware["vendor"]) == (
        *_HARDWARE[task.removesuffix("-agentx")],
        "amd",
    )


def test_agentx_tasks_differ_only_in_their_hardware() -> None:
    small, large = (task_root(_PROJECT, task) for task in _AGENTX_TASKS)
    names = sorted(p.relative_to(small).as_posix() for p in small.rglob("*") if p.is_file())
    assert names == sorted(p.relative_to(large).as_posix() for p in large.rglob("*") if p.is_file())
    differ = [name for name in names if (small / name).read_bytes() != (large / name).read_bytes()]
    assert differ == ["OBJECTIVE.md", "vibesys.input.toml"]
    small_manifest, large_manifest = (_manifest(task) for task in _AGENTX_TASKS)
    small_manifest.pop("catalogue")
    large_manifest.pop("catalogue")
    assert small_manifest == large_manifest


@pytest.mark.parametrize("task", _AGENTX_TASKS)
def test_agentx_task_images_carry_the_shared_service_checker(task: str) -> None:
    shared = _PROJECT / ".vibesys/shared/accuracy_checker/checker.py"
    assert (task_root(_PROJECT, task) / "checker.py").read_bytes() == shared.read_bytes()
    dockerfile = (task_root(_PROJECT, task) / "Dockerfile").read_text()
    assert "\nFROM ghcr.io/uw-syfi/vibesys-evaluator-llm-serving:" in dockerfile
    assert "@sha256:" in dockerfile.split("\nFROM ", 1)[1].split("\n", 1)[0]
    assert "HF_HOME=/vibesys/mounts/hf HF_HUB_OFFLINE=1" in dockerfile


@pytest.mark.parametrize("task", _AGENTX_TASKS)
@pytest.mark.parametrize("mode", ["full", "smoke"])
def test_agentx_bench_replays_the_agentx_v1_recipe(task: str, mode: str, tmp_path: Path) -> None:
    """bench.py's replay is the command the 0.1 tasks' `agentx-v1` evaluator ran, flag for flag."""
    calls: list[dict] = []
    bench = SimpleNamespace(**runpy.run_path(str(task_root(_PROJECT, task) / "bench.py")))
    # This run of bench.py has globals of its own: its `agentx` records the replay it asks for.
    bench.replay.__globals__["agentx"] = SimpleNamespace(
        replay=lambda **kwargs: calls.append(kwargs) or {}
    )
    reports: list[dict] = []
    run = SimpleNamespace(phase=lambda name: tmp_path / name, report=reports.append)
    if mode == "full":
        bench.benchmark(run)
        assert reports == [{}]
    else:
        bench.profile(run)
        assert reports == []
    (kwargs,) = calls
    assert kwargs["mode"] == mode
    assert kwargs.pop("artifacts") == tmp_path / "agentx"
    url = "http://127.0.0.1:9"
    replay = _agentx.Replay(**kwargs, url=url, timeout_secs=3300)
    evaluator = SimpleNamespace(
        **runpy.run_path(
            str(Path(__file__).parents[2] / "resources/evaluators/agentx/agentx_eval.py")
        )
    )
    args = [*_AGENTX_V1_ARGS, "--url", url]
    args[args.index("--mode") + 1] = mode
    expected = evaluator.profile_command(evaluator.parse_args(args), tmp_path)
    assert _agentx.command(replay, tmp_path, python=expected[0]) == expected


_SCALE = "mi2508x-tracelab-scale"


def test_tracelab_scale_task_is_a_two_image_task_of_the_baseline_engine() -> None:
    manifest = _manifest(_SCALE)
    assert manifest["version"] == "0.2"
    assert manifest["backends"] == ["rocm"]
    assert "evaluator" not in manifest
    # Development runs are `submit command` jobs in the task image, not a [profile] section.
    assert "profile" not in manifest
    assert set(manifest["environment"]["needs"]) == {"rocm", "mounts"}
    candidate = manifest["candidate"]
    assert (candidate["dockerfile"], candidate["context"]) == ("Dockerfile.rocm", ".")
    assert (_PROJECT / candidate["dockerfile"]).is_file()
    assert candidate["serve"] == [
        "python", "-m", "engine.server", "--host", "127.0.0.1", "--port", "${PORT}",
    ]  # fmt: skip
    assert (candidate["ready"], candidate["startup_timeout_seconds"]) == ("/health", 1800)
    assert manifest["accuracy"] == {
        "command": ["sh", "-c", 'exec python3 -I /task/checker.py --url "$VIBESYS_SERVICE_URL"'],
        "timeout_seconds": 300,
    }
    assert manifest["benchmark"] == {
        "command": [
            "python", "-I", "/task/tracelab/replay.py",
            "--request-factory-engine", "/usr/local/bin/session_runner",
        ],
        "timeout_seconds": 3600,
        "result_protocol": 2,
    }  # fmt: skip
    hardware = manifest["catalogue"]["hardware"]
    assert (hardware["accelerator"], hardware["count"], hardware["vendor"]) == (
        "AMD MI250",
        8,
        "amd",
    )


def test_tracelab_scale_task_image_carries_the_checker_and_the_adapter() -> None:
    root = task_root(_PROJECT, _SCALE)
    shared = _PROJECT / ".vibesys/shared/accuracy_checker/checker.py"
    assert (root / "checker.py").read_bytes() == shared.read_bytes()
    dockerfile = (root / "Dockerfile").read_text()
    assert "HF_HOME=/vibesys/mounts/hf HF_HUB_OFFLINE=1" in dockerfile
    assert "COPY request-factory/windowed.patch" in dockerfile
    scale = SimpleNamespace(**runpy.run_path(str(root / "tracelab/replay.py")))
    objectives = tomllib.loads((root / "objectives.toml").read_text())
    assert [item["name"] for item in objectives["objective"]] == scale.OBJECTIVES
    assert scale.REVISION in (root / "reference/meta.json").read_text()
    trace = root / "tracelab/trace.csv"
    assert hashlib.sha256(trace.read_bytes()).hexdigest() == scale.TRACE_SHA256


def test_tracelab_scale_task_publishes_no_held_out_material() -> None:
    root = task_root(_PROJECT, _SCALE)
    for path in root.rglob("*"):
        if path.is_file() and path.name != "windowed.patch" and path.suffix != ".csv":
            text = path.read_text(errors="ignore").lower()
            assert "holdout-private" not in text, path
            assert "qwen36-scale-holdout" not in text, path


def test_the_baseline_engine_serves_the_checkpoint_every_task_pins() -> None:
    pinned = json.loads((_PROJECT / "engine/checkpoint.json").read_text())
    assert pinned == {"model": _REPLAY.MODEL, "revision": _REPLAY.REVISION}
    metas = list((_PROJECT / ".vibesys/tasks").glob("*/reference/meta.json"))
    assert metas
    for meta in metas:
        assert json.loads(meta.read_text()) == {
            "model_id": pinned["model"],
            "revision": pinned["revision"],
        }


def _pins(lines: list[str]) -> dict[str, str]:
    pins = {}
    for line in lines:
        name, sep, version = line.split("#", 1)[0].strip().strip('",').partition("==")
        if sep:
            pins[name] = version
    return pins


def test_the_engine_environments_pin_the_same_versions() -> None:
    """The CUDA, ROCm and CPU-check environments differ only in their torch build."""
    cuda = _pins((_PROJECT / "requirements.txt").read_text().splitlines())
    rocm = _pins((_PROJECT / "requirements-agentx.txt").read_text().splitlines())
    check = tomllib.loads((_PROJECT / ".vibesys/shared/cpu_check/pyproject.toml").read_text())
    cpu = _pins(check["project"]["dependencies"])
    assert cuda.pop("torch") == cpu.pop("torch") == "2.9.1"
    assert cuda == rocm == cpu


def test_recorded_sample_is_pinned_and_contains_every_round() -> None:
    path = _TRACE / "recorded_sessions.csv"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == _REPLAY.TRACE_SHA256
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 303
    assert len({row["session_id"] for row in rows}) == 48
    assert sum(int(row["output_len"]) for row in rows) == 58678
    assert any(float(row["tool_wait_after_ms"]) > 0 for row in rows)
    assert any(int(row["prefix_len"]) > 0 for row in rows)


def _summary(value: object = 1.0) -> dict:
    return {
        "replay": {
            "kind": "sessions",
            "common": {
                "attempted_steps": 303,
                "success_steps": 303,
                "failed_steps": 0,
                "actual_output_tokens": 58678,
                "target_output_tokens": 58678,
                **dict.fromkeys(_REPLAY.METRICS.values(), value),
            },
        },
    }


@given(value=st.floats(min_value=0.001, max_value=1e10, allow_nan=False))
def test_trace_result_preserves_native_measured_values(value: float) -> None:
    assert _REPLAY.summary_metrics(_summary(value), 303, 58678) == dict.fromkeys(
        _REPLAY.METRICS, value
    )


@pytest.mark.parametrize("field", list(_REPLAY.METRICS.values()))
@pytest.mark.parametrize("value", [None, True, "1", -1, float("nan"), float("inf")])
def test_trace_result_rejects_invalid_metrics(field: str, value: object) -> None:
    summary = _summary()
    summary["replay"]["common"][field] = value
    with pytest.raises(ValueError, match=field):
        _REPLAY.summary_metrics(summary, 303, 58678)


@pytest.mark.parametrize(
    "field",
    [
        "attempted_steps",
        "success_steps",
        "failed_steps",
        "actual_output_tokens",
        "target_output_tokens",
    ],
)
@given(value=st.one_of(st.booleans(), st.integers(min_value=1, max_value=302)))
def test_trace_result_rejects_incomplete_or_failed_runs(field: str, value: object) -> None:
    summary = _summary()
    summary["replay"]["common"][field] = value
    with pytest.raises(ValueError, match=field):
        _REPLAY.summary_metrics(summary, 303, 58678)


@pytest.mark.parametrize(
    "summary",
    [None, {}, {"replay": {}}, {"replay": {"kind": "independent_requests", "common": {}}}],
)
def test_trace_result_requires_session_metrics(summary: object) -> None:
    with pytest.raises(ValueError, match="replay"):
        _REPLAY.summary_metrics(summary, 303, 58678)
