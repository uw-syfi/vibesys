"""AMD workload tasks of the Qwen3.6 example and the TraceLab adapter they replay with."""

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
from tests.examples.example_task import task_root

from vibesys_evaluator.llm_serving import _agentx

_REPO = Path(__file__).parents[2]
_PROJECT = _REPO / "examples/model-serving/qwen3.6-35b-a3b"
_SHARED_CHECKER = _PROJECT / ".vibesys/shared/accuracy_checker/checker.py"
_HARDWARE = {"mi2104x": ("AMD MI210", 4), "mi300x": ("AMD MI300X", 1)}
_AGENTX_TASKS = [f"{hardware}-agentx" for hardware in _HARDWARE]
_TRACELAB_TASKS = [f"{hardware}-tracelab" for hardware in _HARDWARE]
_TRACE = task_root(_PROJECT, _TRACELAB_TASKS[0]) / "tracelab"
_REPLAY = SimpleNamespace(**runpy.run_path(str(_TRACE / "replay.py")))
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


@pytest.mark.parametrize("workload", ["agentx", "tracelab"])
def test_amd_tasks_score_the_same_five_objectives(workload: str) -> None:
    for hardware in _HARDWARE:
        root = task_root(_PROJECT, f"{hardware}-{workload}")
        objectives = tomllib.loads((root / "objectives.toml").read_text())
        assert {item["name"] for item in objectives["objective"]} == set(_REPLAY.METRICS)
        assert objectives["pareto"]["relative_noise"] == 0.03
        assert _REPLAY.REVISION in (root / "reference/meta.json").read_text()


# The task-image command each workload's benchmark runs; vibesys-cli appends --vs-output FILE.
_BENCHMARKS = {
    "agentx": ["python", "/task/bench.py", "benchmark"],
    "tracelab": [
        "python", "-I", "/task/tracelab/replay.py",
        "--request-factory-engine", "/usr/local/bin/session_runner",
    ],
}  # fmt: skip


@pytest.mark.parametrize("task", _AGENTX_TASKS + _TRACELAB_TASKS)
def test_amd_tasks_are_two_image_tasks_of_the_baseline_engine(task: str) -> None:
    hardware, workload = task.split("-")
    manifest = _manifest(task)
    assert manifest["version"] == "0.2"
    assert manifest["backends"] == ["rocm"]
    assert "evaluator" not in manifest
    # [profile] is not part of these tasks: development runs are commands in the task image.
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
        "command": _BENCHMARKS[workload],
        "timeout_seconds": 10800 if workload == "agentx" else 3600,
        "result_protocol": 2,
    }
    declared = manifest["catalogue"]["hardware"]
    assert (declared["accelerator"], declared["count"], declared["vendor"]) == (
        *_HARDWARE[hardware],
        "amd",
    )


@pytest.mark.parametrize("tasks", [_AGENTX_TASKS, _TRACELAB_TASKS])
def test_a_workloads_tasks_differ_only_in_their_hardware(tasks: list[str]) -> None:
    small, large = (task_root(_PROJECT, task) for task in tasks)
    names = sorted(p.relative_to(small).as_posix() for p in small.rglob("*") if p.is_file())
    assert names == sorted(p.relative_to(large).as_posix() for p in large.rglob("*") if p.is_file())
    differ = [name for name in names if (small / name).read_bytes() != (large / name).read_bytes()]
    assert differ == ["OBJECTIVE.md", "vibesys.input.toml"]
    small_manifest, large_manifest = (_manifest(task) for task in tasks)
    small_manifest.pop("catalogue")
    large_manifest.pop("catalogue")
    assert small_manifest == large_manifest


def test_every_task_image_carries_the_shared_service_checker() -> None:
    tasks = sorted(path.name for path in (_PROJECT / ".vibesys/tasks").iterdir())
    assert tasks == sorted([*_AGENTX_TASKS, *_TRACELAB_TASKS, "mi2508x-tracelab-scale"])
    for task in tasks:
        copy = task_root(_PROJECT, task) / "checker.py"
        assert copy.read_bytes() == _SHARED_CHECKER.read_bytes(), task


@pytest.mark.parametrize("task", _AGENTX_TASKS)
def test_agentx_task_images_build_from_the_pinned_evaluator_image(task: str) -> None:
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
    assert kwargs.pop("timeout_secs") == 10500.0
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


_ENGINE = "/usr/local/bin/session_runner"
_RF_PACKAGE = _REPO / "resources/evaluators/request-factory"
# The session_runner command line of the schema-0.1 TraceLab tasks' replay.py
# (.vibesys/shared/tracelab/replay.py, run by the `request-factory-adapter` entrypoint), with its
# temporary paths as placeholders; the schema-0.2 tasks must run exactly this replay.
_RF_0_1_COMMAND = [
    _ENGINE,
    "--trace", "{tmp}/trace.csv",
    "--input-file-format", "text-generation-session-execution-v2",
    "--text-file", "{tmp}/corpus.txt",
    "--tokenizer", "{tokenizer}",
    "--token-pool-limit", "1000000",
    "--base-url", "http://127.0.0.1:9/v1",
    "--model", "Qwen/Qwen3.6-35B-A3B",
    "--backend", "openai",
    "--temperature", "0",
    "--arrival-mode", "saturated",
    "--max-concurrency", "8",
    "--max-model-len", "262144",
    "--request-log", "false",
    "--timeline", "false",
    "--summary-path", "{tmp}/summary.json",
]  # fmt: skip


@pytest.mark.parametrize("task", _TRACELAB_TASKS)
def test_tracelab_task_images_build_the_evaluators_session_runner(task: str) -> None:
    """The image's runner is the 0.1 evaluator's tool: same repository, revision and binary."""
    tool = tomllib.loads((_RF_PACKAGE / "vibesys.evaluator.toml").read_text())["tools"]
    tool = tool["request-factory"]
    dockerfile = (task_root(_PROJECT, task) / "Dockerfile").read_text()
    assert f"ARG REQUEST_FACTORY_REV={tool['rev']}\n" in dockerfile
    assert f"fetch --depth 1 {tool['git']} " in dockerfile
    assert tool["bins"] == ["session_runner"]
    assert "--bin session_runner" in dockerfile
    assert f"COPY --from=request-factory /opt/request-factory/bin/session_runner {_ENGINE}\n" in (
        dockerfile
    )
    # The recorded-sample replay needs no patched runner; the scale task's windows do.
    assert "patch" not in dockerfile.lower()
    assert "HF_HOME=/vibesys/mounts/hf HF_HUB_OFFLINE=1" in dockerfile
    for line in dockerfile.splitlines():
        if line.startswith("FROM "):
            assert "@sha256:" in line, line


@pytest.mark.parametrize("task", _TRACELAB_TASKS)
def test_tracelab_benchmark_launches_the_replay_as_the_0_1_adapter_did(task: str) -> None:
    """`request-factory-adapter` execs `python <script> --request-factory-engine <runner>`."""
    calls: list[list[str]] = []
    adapter = SimpleNamespace(**runpy.run_path(str(_RF_PACKAGE / "adapter.py")))
    adapter.main.__globals__["os"] = SimpleNamespace(
        environ={}, execve=lambda _path, argv, _env: calls.append(argv)
    )
    script = "/task/tracelab/replay.py"
    adapter.main(["--engine", _ENGINE, "--", script])
    (argv,) = calls
    command = _manifest(task)["benchmark"]["command"]
    assert command[:3] == ["python", "-I", script]
    assert argv[1:] == [script, *command[3:]]


def test_tracelab_replay_runs_the_0_1_replay_flag_for_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same runner command, same trace rows (with the runner's canonical ids), same token pool."""
    tokenizer = (
        tmp_path / "hf/hub/models--Qwen--Qwen3.6-35B-A3B/snapshots" / _REPLAY.REVISION
    ) / "tokenizer.json"
    tokenizer.parent.mkdir(parents=True)
    tokenizer.write_text("{}")
    monkeypatch.setenv("HF_HOME", str(tmp_path / "hf"))
    work = tmp_path / "work"
    work.mkdir()
    commands: list[list[str]] = []

    def run(command: list[str], *, check: bool, timeout: int) -> None:
        assert (check, timeout) == (True, 3000)
        commands.append(command)
        Path(command[command.index("--summary-path") + 1]).write_text(json.dumps(_summary()))

    args = SimpleNamespace(request_factory_engine=_ENGINE, url="http://127.0.0.1:9/")
    assert _REPLAY.run_replay(args, work, run) == dict.fromkeys(_REPLAY.METRICS, 1.0)
    (command,) = commands
    assert command == [part.format(tmp=work, tokenizer=tokenizer) for part in _RF_0_1_COMMAND]
    with (_TRACE / "recorded_sessions.csv").open(newline="") as stream:
        recorded = list(csv.DictReader(stream))
    with (work / "trace.csv").open(newline="") as stream:
        replayed = list(csv.DictReader(stream))
    assert [{k: v for k, v in row.items() if k in recorded[0]} for row in replayed] == recorded
    assert [row["arrival_time_ms"] for row in replayed] == ["0"] * len(recorded)
    assert [row["request_id"] for row in replayed] == [
        f"session_{row['session_id']}_round_{int(row['round_idx']):06d}" for row in recorded
    ]
    corpus = (work / "corpus.txt").read_text().splitlines()
    assert len(corpus) == 200_000
    assert corpus[0].startswith("def case_0(items):")


def test_tracelab_replay_needs_the_hugging_face_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    args = SimpleNamespace(request_factory_engine=_ENGINE, url="http://127.0.0.1:9")
    with pytest.raises(ValueError, match="missing from the Hugging Face cache"):
        _REPLAY.run_replay(args, tmp_path)


@pytest.mark.parametrize("outcome", ["result", "error"])
def test_tracelab_replay_writes_a_protocol_2_stream(
    outcome: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def replay(args: SimpleNamespace, _temporary: Path) -> dict[str, float]:
        # The server address comes from vibesys-cli's environment when no --url is given.
        assert (args.url, args.request_factory_engine) == ("http://127.0.0.1:9", _ENGINE)
        if outcome == "error":
            message = "summary.replay must be an object"
            raise ValueError(message)
        return dict.fromkeys(_REPLAY.METRICS, 2.0)

    monkeypatch.setenv("VIBESYS_SERVICE_URL", "http://127.0.0.1:9")
    output = tmp_path / "out.jsonl"
    output.write_text("stale\n")
    argv = ["--request-factory-engine", _ENGINE, "--vs-output", str(output)]
    assert _REPLAY.main(argv, replay) == (0 if outcome == "result" else 1)
    hello, record = (json.loads(line) for line in output.read_text().splitlines())
    objectives = tomllib.loads((_TRACE.parent / "objectives.toml").read_text())
    assert hello == {
        "kind": "hello",
        "protocol": 2,
        # Exactly the task's objectives, with their directions and units.
        "metrics": {
            item["name"]: {"unit": item["unit"], "direction": item["direction"]}
            for item in objectives["objective"]
        },
    }
    if outcome == "result":
        assert record == {
            "kind": "result",
            "label": "",
            "values": dict.fromkeys(hello["metrics"], 2.0),
        }
    else:
        assert record == {"kind": "error", "message": "summary.replay must be an object"}


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


def test_tracelab_scale_task_image_carries_the_adapter() -> None:
    root = task_root(_PROJECT, _SCALE)
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
