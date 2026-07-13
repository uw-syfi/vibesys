"""Tests for the unified ``vibeserve`` CLI dispatcher."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from vibe_serve.cli import _extract_flag, _extract_loop_selection, _validate_target_inputs, main
from vibe_serve.domains.base import DomainName
from vibe_serve.errors import ConfigurationError
from vibe_serve.profilers import ProfilerKind

TARGET_ARGS = ["--input", "examples/model-serving/Llama-3-8B"]


def _write_input_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "queue-spsc"
    (bundle / "reference").mkdir(parents=True)
    (bundle / "OBJECTIVE.md").write_text("objective\n")
    (bundle / "vibeserve.input.toml").write_text(
        """
version = 1

[agent]
domain = "generic"

[accuracy]
command = ["uv", "run", "python", "accuracy_checker/checker.py"]

[benchmark]
command = ["uv", "run", "python", "benchmark/benchmark.py"]
""".lstrip()
    )
    return bundle


# ---------------------------------------------------------------------------
# Flag extraction
# ---------------------------------------------------------------------------


def test_extract_flag_space_form():
    val, rest = _extract_flag(["--outer-loop", "agent", "--input", "x"], "--outer-loop")
    assert val == "agent"
    assert rest == ["--input", "x"]


def test_extract_flag_equals_form():
    val, rest = _extract_flag(["--input", "x", "--outer-loop=evolve"], "--outer-loop")
    assert val == "evolve"
    assert rest == ["--input", "x"]


def test_extract_flag_missing_returns_none():
    val, rest = _extract_flag(["--input", "x"], "--outer-loop")
    assert val is None
    assert rest == ["--input", "x"]


def test_extract_flag_dangling_exits():
    with pytest.raises(ConfigurationError) as exc:
        _extract_flag(["--outer-loop"], "--outer-loop")
    assert exc.value.diagnostic.code == "invalid_arguments"


# ---------------------------------------------------------------------------
# argv -> loop kind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv,expected_kind,expected_rest",
    [
        (["--outer-loop", "agent", "--input", "x"], "agent", ["--input", "x"]),
        (["--outer-loop", "plain", "--exp-name", "e"], "plain", ["--exp-name", "e"]),
        (["--outer-loop", "evolve", "--seed", "1"], "evolve", ["--seed", "1"]),
    ],
)
def test_extract_loop_selection(argv: list[str], expected_kind: str, expected_rest: list[str]):
    kind, rest = _extract_loop_selection(argv)
    assert kind == expected_kind
    assert rest == expected_rest


def test_extract_loop_selection_defaults_to_agent():
    kind, rest = _extract_loop_selection(["--input", "x"])
    assert kind == "agent"
    assert rest == ["--input", "x"]


def test_extract_loop_selection_unknown_outer_loop_exits():
    with pytest.raises(ConfigurationError) as exc:
        _extract_loop_selection(["--outer-loop", "nope"])
    assert exc.value.diagnostic.stage == "argument_parsing"


def test_target_input_defaults_to_none():
    from vibe_serve.cli import _build_agent_parser

    args = _build_agent_parser().parse_args([])

    assert args.input is None
    assert not hasattr(args, "ref")
    assert not hasattr(args, "acc_checker")
    assert not hasattr(args, "bench")
    assert args.profiler is ProfilerKind.AUTO
    assert not hasattr(args, "profiler_support")
    assert not hasattr(args, "domain")


@pytest.mark.parametrize(
    "obsolete_flag",
    ["--profiler-support", "--nsys-profiler", "--torch-profiler", "--neuron-profiler"],
)
def test_profiler_support_override_flags_are_rejected(obsolete_flag):
    from vibe_serve.cli import _build_agent_parser

    with pytest.raises(ConfigurationError, match="unrecognized arguments"):
        _build_agent_parser().parse_args([obsolete_flag, "support"])


@pytest.mark.parametrize(
    "builder_name",
    [
        "_build_agent_parser",
        "_build_evolve_parser",
        "_build_openevolve_parser",
        "_build_plain_parser",
    ],
)
def test_input_arg_is_available_on_all_loop_parsers(builder_name):
    import vibe_serve.cli as cli

    parser = getattr(cli, builder_name)()
    args = parser.parse_args(["--input", "examples/data-structures/queue-spsc"])

    assert args.input == Path("examples/data-structures/queue-spsc")


@pytest.mark.parametrize(
    "builder_name,validator_name",
    [
        ("_build_agent_parser", "_validate_agent"),
        ("_build_evolve_parser", "_validate_evolve"),
        ("_build_openevolve_parser", "_validate_openevolve"),
        ("_build_plain_parser", "_validate_plain"),
    ],
)
def test_profiler_none_is_valid_with_modal(builder_name, validator_name, tmp_path):
    import vibe_serve.cli as cli

    bundle = _write_input_bundle(tmp_path)
    parser = getattr(cli, builder_name)()
    validator = getattr(cli, validator_name)
    args = parser.parse_args(["--modal", "--profiler", "none", "--input", str(bundle)])

    assert args.profiler is ProfilerKind.NONE
    validator(args)
    assert args.input_bundle.root == bundle.resolve()


@pytest.mark.parametrize(
    "argv",
    [
        ["--profiler", "bogus"],
    ],
)
def test_agent_parser_rejects_invalid_enum_args(argv):
    from vibe_serve.cli import _build_agent_parser

    with pytest.raises(ConfigurationError) as exc:
        _build_agent_parser().parse_args(argv)

    assert exc.value.diagnostic.code == "invalid_arguments"


def test_agent_parser_rejects_obsolete_target_flags():
    from vibe_serve.cli import _build_agent_parser

    with pytest.raises(ConfigurationError):
        _build_agent_parser().parse_args(["--ref", "examples/Llama-3-8B/reference"])


def test_validate_target_inputs_loads_manifest(tmp_path):
    from vibe_serve.cli import _build_agent_parser

    bundle = _write_input_bundle(tmp_path)
    args = _build_agent_parser().parse_args(["--input", str(bundle)])

    _validate_target_inputs(args)

    assert args.input_bundle.root == bundle.resolve()
    assert args.input_bundle.domain is DomainName.GENERIC
    assert args.input_bundle.accuracy_command_display == "uv run python accuracy_checker/checker.py"
    assert args.input_bundle.benchmark_command_display == "uv run python benchmark/benchmark.py"


def test_agent_parser_rejects_domain_override_flag():
    from vibe_serve.cli import _build_agent_parser

    with pytest.raises(ConfigurationError):
        _build_agent_parser().parse_args(["--domain", "llm-serving"])


def test_validate_target_inputs_loads_trusted_benchmark_result_contract(tmp_path):
    from vibe_serve.cli import _build_agent_parser

    bundle = _write_input_bundle(tmp_path)
    manifest = bundle / "vibeserve.input.toml"
    manifest.write_text(
        manifest.read_text()
        + "\n[benchmark.result]\njson_argument = '--output-json'\nmetric = 'ops_per_sec'\n"
    )
    args = _build_agent_parser().parse_args(["--input", str(bundle)])

    _validate_target_inputs(args)

    assert args.input_bundle.benchmark_result is not None
    assert args.input_bundle.benchmark_result.json_argument == "--output-json"
    assert args.input_bundle.benchmark_result.metric == "ops_per_sec"


def test_validate_target_inputs_rejects_missing_input_dir(tmp_path):
    from vibe_serve.cli import _build_agent_parser

    missing = tmp_path / "missing"
    args = _build_agent_parser().parse_args(["--input", str(missing)])

    with pytest.raises(ConfigurationError) as exc:
        _validate_target_inputs(args)

    assert "--input path does not exist" in exc.value.diagnostic.message


def test_validate_target_inputs_reports_missing_manifest(tmp_path):
    from vibe_serve.cli import _build_agent_parser

    bundle = tmp_path / "incomplete"
    bundle.mkdir()
    (bundle / "OBJECTIVE.md").write_text("objective\n")

    args = _build_agent_parser().parse_args(["--input", str(bundle)])

    with pytest.raises(ConfigurationError) as exc:
        _validate_target_inputs(args)

    assert "vibeserve.input.toml" in exc.value.diagnostic.message


def test_validate_target_inputs_reports_missing_command(tmp_path):
    from vibe_serve.cli import _build_agent_parser

    bundle = _write_input_bundle(tmp_path)
    tools = bundle / "tools"
    tools.mkdir()
    (tools / "check").write_text("#!/usr/bin/env bash\nexit 0\n")
    (tools / "check").chmod(0o755)
    (bundle / "vibeserve.input.toml").write_text(
        """
version = 1

[agent]
domain = "generic"

[accuracy]
command = ["./tools/check"]

[benchmark]
command = ["./tools/bench"]
""".lstrip()
    )
    args = _build_agent_parser().parse_args(["--input", str(bundle)])

    with pytest.raises(ConfigurationError) as exc:
        _validate_target_inputs(args)

    assert "benchmark.command executable does not exist" in exc.value.diagnostic.message


def test_validate_target_inputs_requires_input():
    from vibe_serve.cli import _build_agent_parser

    args = _build_agent_parser().parse_args([])

    with pytest.raises(ConfigurationError) as exc:
        _validate_target_inputs(args)

    assert "missing required target input: --input" in exc.value.diagnostic.message


# ---------------------------------------------------------------------------
# main() routes to the right runner
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "loop_name,runner_attr",
    [
        ("agent", "_run_agent"),
        ("evolve", "_run_evolve"),
        ("plain", "_run_plain"),
    ],
)
def test_main_routes_to_runner(loop_name: str, runner_attr: str):
    argv = ["vibe-serve", "--outer-loop", loop_name, "--exp-name", "x", *TARGET_ARGS]
    with patch.object(sys, "argv", argv), patch(f"vibe_serve.cli.{runner_attr}") as runner:
        main()
        runner.assert_called_once()
        args = runner.call_args.args[0]
        assert args.exp_name == "x"
        assert args.input_bundle.root.name == "Llama-3-8B"


def test_main_wraps_tty_run_in_tui():
    argv = [
        "vibe-serve",
        "--outer-loop",
        "agent",
        "--exp-name",
        "x",
        *TARGET_ARGS,
    ]
    with (
        patch.object(sys, "argv", argv),
        patch.object(sys.stdin, "isatty", return_value=True),
        patch.object(sys.stdout, "isatty", return_value=True),
        patch("vibe_serve.cli._run_agent") as runner,
        patch("vibe_serve.launcher.launch", return_value=0) as launch,
    ):
        with pytest.raises(SystemExit) as exc:
            main()

    assert exc.value.code == 0
    runner.assert_not_called()
    launch.assert_called_once_with(argv[1:])


def test_main_headless_skips_tui():
    argv = [
        "vibe-serve",
        "--outer-loop",
        "agent",
        "--headless",
        *TARGET_ARGS,
    ]
    with (
        patch.object(sys, "argv", argv),
        patch("vibe_serve.cli._run_agent") as runner,
        patch("vibe_serve.launcher.launch") as launch,
    ):
        main()
    runner.assert_called_once()
    launch.assert_not_called()
