"""Tests for synthesizing input bundles from standalone CLI flags."""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import pytest

from vibesys.constants import DomainName
from vibesys.errors import ConfigurationError
from vibesys.evaluators.input_manifest import load_input_bundle
from vibesys.evaluators.input_synthesis import (
    InputSynthesisError,
    SynthesizedInputSpec,
    synthesize_input_bundle,
)

if TYPE_CHECKING:
    from pathlib import Path

_BASE_SPEC = SynthesizedInputSpec(
    objective="Serve the model quickly.",
    domain=DomainName.LLM_SERVING,
    accuracy_command=("python", "checker.py"),
    benchmark_command=("python", "benchmark.py"),
)


def _minimal_spec(**overrides: object) -> SynthesizedInputSpec:
    return dataclasses.replace(_BASE_SPEC, **overrides)


def test_synthesize_minimal_bundle_round_trips(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    root = synthesize_input_bundle(_minimal_spec(), tmp_path / "bundle")

    bundle = load_input_bundle(root)

    assert bundle.domain == DomainName.LLM_SERVING
    assert bundle.objective == "Serve the model quickly.\n"
    assert bundle.accuracy_command == ("python", "checker.py")
    assert bundle.benchmark_command == ("python", "benchmark.py")
    assert bundle.reference_path is None
    assert bundle.benchmark_result is None


def test_synthesize_populates_optional_fields(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    reference = tmp_path / "ref"
    reference.mkdir()
    (reference / "golden.txt").write_text("42\n")

    spec = _minimal_spec(
        accuracy_timeout_seconds=120,
        benchmark_timeout_seconds=300,
        benchmark_metric="latency_ms",
        benchmark_result_arg="--result-json",
        reference_dir=reference,
    )
    root = synthesize_input_bundle(spec, tmp_path / "bundle")

    bundle = load_input_bundle(root)

    assert bundle.manifest.accuracy.timeout_seconds == 120
    assert bundle.manifest.benchmark.timeout_seconds == 300
    assert bundle.benchmark_result is not None
    assert bundle.benchmark_result.metric == "latency_ms"
    assert bundle.benchmark_result.json_argument == "--result-json"
    assert bundle.reference_path == (root / "reference").resolve()
    assert bundle.reference_path is not None
    assert (bundle.reference_path / "golden.txt").read_text() == "42\n"


def test_synthesize_copies_evaluator_dir_contents_into_root(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    evaluator = tmp_path / "eval"
    (evaluator / "pkg").mkdir(parents=True)
    (evaluator / "checker.py").write_text("print('ok')\n")
    (evaluator / "pkg" / "helper.py").write_text("X = 1\n")

    root = synthesize_input_bundle(_minimal_spec(evaluator_dir=evaluator), tmp_path / "bundle")

    assert (root / "checker.py").read_text() == "print('ok')\n"
    assert (root / "pkg" / "helper.py").read_text() == "X = 1\n"
    # The manifest and objective the synthesizer owns are still intact.
    load_input_bundle(root)


def test_synthesize_rejects_evaluator_dir_reserved_name_collision(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    evaluator = tmp_path / "eval"
    (evaluator / "reference").mkdir(parents=True)

    with pytest.raises(InputSynthesisError, match="reserved bundle name"):
        synthesize_input_bundle(_minimal_spec(evaluator_dir=evaluator), tmp_path / "bundle")


def test_synthesize_requires_both_benchmark_result_fields(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    with pytest.raises(InputSynthesisError, match="both --input-benchmark-metric"):
        synthesize_input_bundle(_minimal_spec(benchmark_metric="latency_ms"), tmp_path / "bundle")


def test_synthesize_refuses_existing_destination(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    dest = tmp_path / "bundle"
    dest.mkdir()

    with pytest.raises(InputSynthesisError, match="already exists"):
        synthesize_input_bundle(_minimal_spec(), dest)


def test_synthesize_rejects_missing_source_dir(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    with pytest.raises(InputSynthesisError, match="--input-reference path does not exist"):
        synthesize_input_bundle(_minimal_spec(reference_dir=tmp_path / "nope"), tmp_path / "bundle")


def test_synthesize_escapes_special_characters_in_commands(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    spec = _minimal_spec(
        objective='Handle "quotes" and\nnewlines.',
        accuracy_command=("python", "-c", 'print("hi")'),
    )
    root = synthesize_input_bundle(spec, tmp_path / "bundle")

    bundle = load_input_bundle(root)
    assert bundle.accuracy_command == ("python", "-c", 'print("hi")')
    assert bundle.objective == 'Handle "quotes" and\nnewlines.\n'


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def _agent_args(argv: list[str]):  # noqa: ANN202  # tracked: #288
    from entrypoints import cli  # noqa: PLC0415  # tracked: #288

    return cli._build_agent_parser().parse_args(argv)  # noqa: SLF001  # tracked: #288


def test_standalone_flags_synthesize_bundle(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    from entrypoints import cli  # noqa: PLC0415  # tracked: #288

    args = _agent_args(
        [
            "--input-objective",
            "Serve fast.",
            "--input-domain",
            "llm-serving",
            "--input-accuracy-command",
            "python checker.py",
            "--input-benchmark-command",
            "python benchmark.py",
            "--no-skills",
            "--runs-dir",
            str(tmp_path / "selected-runs"),
        ]
    )

    args.runs_dir = args.runs_dir.resolve()

    cli._validate_target_inputs(args)  # noqa: SLF001  # tracked: #288

    assert args.exp_name.startswith("llm-serving-")
    assert args.input == tmp_path / "selected-runs" / "_inputs" / args.exp_name
    assert args.input_bundle.domain == DomainName.LLM_SERVING
    assert args.input_bundle.accuracy_command == ("python", "checker.py")


@pytest.mark.parametrize(
    "unsafe_name",
    ["/absolute", "nested/name", ".", ".."],
)
def test_standalone_flags_reject_unsafe_fresh_experiment_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsafe_name: str,
) -> None:
    from entrypoints import cli  # noqa: PLC0415  # tracked: #288

    monkeypatch.setattr(cli.sys, "prefix", str(tmp_path / ".venv"))
    runs_dir = tmp_path / "selected-runs"
    exp_name = str(tmp_path / "outside") if unsafe_name == "/absolute" else unsafe_name

    with pytest.raises(ConfigurationError) as exc:
        cli.parse_cli_invocation(
            [
                "--outer-loop",
                "agent",
                "--runs-dir",
                str(runs_dir),
                "--exp-name",
                exp_name,
                "--input-objective",
                "Serve fast.",
                "--input-domain",
                "llm-serving",
                "--input-accuracy-command",
                "python checker.py",
                "--input-benchmark-command",
                "python benchmark.py",
                "--no-skills",
            ]
        )

    assert exc.value.diagnostic.code == "invalid_exp_name"
    assert not runs_dir.exists()
    assert not (tmp_path / "outside").exists()


def test_objective_file_is_read(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    from entrypoints import cli  # noqa: PLC0415  # tracked: #288

    objective_file = tmp_path / "OBJ.md"
    objective_file.write_text("From a file.\n")
    args = _agent_args(
        [
            "--input-objective-file",
            str(objective_file),
            "--input-domain",
            "generic",
            "--input-accuracy-command",
            "checker",
            "--input-benchmark-command",
            "bench",
            "--runs-dir",
            str(tmp_path / "runs"),
        ]
    )

    cli._validate_target_inputs(args)  # noqa: SLF001  # tracked: #288

    assert args.input_bundle.objective == "From a file.\n"


def test_input_conflicts_with_standalone_flags(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    args = _agent_args(["--input", str(tmp_path), "--input-domain", "generic"])

    from entrypoints import cli  # noqa: PLC0415  # tracked: #288

    with pytest.raises(ConfigurationError, match="cannot be combined"):
        cli._validate_target_inputs(args)  # noqa: SLF001  # tracked: #288


def test_removed_hidden_evaluator_flag_is_rejected() -> None:
    with pytest.raises(ConfigurationError) as exc:
        _agent_args(["--input-hidden-evaluator-source", "evaluator"])

    assert exc.value.diagnostic.code == "invalid_arguments"


def test_removed_workspace_seed_flag_is_rejected() -> None:
    with pytest.raises(ConfigurationError) as exc:
        _agent_args(["--input-workspace-seed", "candidate"])

    assert exc.value.diagnostic.code == "invalid_arguments"


def test_missing_current_project_and_standalone_flags_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    args = _agent_args([])

    from entrypoints import cli  # noqa: PLC0415  # tracked: #288

    with pytest.raises(ConfigurationError, match="Current directory is not a VibeSys project"):
        cli._validate_target_inputs(args)  # noqa: SLF001  # tracked: #288


def test_incomplete_standalone_flags_error():  # noqa: ANN201  # tracked: #288
    # Objective + domain but no evaluator commands.
    args = _agent_args(["--input-objective", "x", "--input-domain", "generic"])

    from entrypoints import cli  # noqa: PLC0415  # tracked: #288

    with pytest.raises(ConfigurationError, match="standalone input requires"):
        cli._validate_target_inputs(args)  # noqa: SLF001  # tracked: #288


def test_both_objective_forms_rejected(tmp_path):  # noqa: ANN001, ANN201  # tracked: #288
    from entrypoints import cli  # noqa: PLC0415  # tracked: #288

    objective_file = tmp_path / "OBJ.md"
    objective_file.write_text("file\n")
    args = _agent_args(
        [
            "--input-objective",
            "inline",
            "--input-objective-file",
            str(objective_file),
            "--input-domain",
            "generic",
            "--input-accuracy-command",
            "checker",
            "--input-benchmark-command",
            "bench",
        ]
    )

    with pytest.raises(ConfigurationError, match="only one of --input-objective"):
        cli._validate_target_inputs(args)  # noqa: SLF001  # tracked: #288
