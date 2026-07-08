"""Tests for the unified ``vibeserve`` CLI dispatcher."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from vibe_serve.cli import _extract_flag, _extract_loop_selection, _validate_target_inputs, main
from vibe_serve.domains.base import DomainName
from vibe_serve.profilers import ProfilerKind

TARGET_ARGS = [
    "--ref",
    "examples/Llama-3-8B/reference",
    "--acc-checker",
    "examples/Llama-3-8B/accuracy_checker",
    "--bench",
    "examples/Llama-3-8B/benchmark",
]

# ---------------------------------------------------------------------------
# Flag extraction
# ---------------------------------------------------------------------------


def test_extract_flag_space_form():
    val, rest = _extract_flag(["--outer-loop", "agent", "--ref", "x"], "--outer-loop")
    assert val == "agent"
    assert rest == ["--ref", "x"]


def test_extract_flag_equals_form():
    val, rest = _extract_flag(["--ref", "x", "--outer-loop=evolve"], "--outer-loop")
    assert val == "evolve"
    assert rest == ["--ref", "x"]


def test_extract_flag_missing_returns_none():
    val, rest = _extract_flag(["--ref", "x"], "--outer-loop")
    assert val is None
    assert rest == ["--ref", "x"]


def test_extract_flag_dangling_exits():
    with pytest.raises(SystemExit) as exc:
        _extract_flag(["--outer-loop"], "--outer-loop")
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# argv → loop kind
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv,expected_kind,expected_rest",
    [
        (["--outer-loop", "agent", "--ref", "x"], "agent", ["--ref", "x"]),
        (["--outer-loop", "plain", "--exp-name", "e"], "plain", ["--exp-name", "e"]),
        (["--outer-loop", "evolve", "--seed", "1"], "evolve", ["--seed", "1"]),
    ],
)
def test_extract_loop_selection(argv: list[str], expected_kind: str, expected_rest: list[str]):
    kind, rest = _extract_loop_selection(argv)
    assert kind == expected_kind
    assert rest == expected_rest


def test_extract_loop_selection_defaults_to_agent():
    kind, rest = _extract_loop_selection(["--ref", "x"])
    assert kind == "agent"
    assert rest == ["--ref", "x"]


def test_extract_loop_selection_unknown_outer_loop_exits():
    with pytest.raises(SystemExit) as exc:
        _extract_loop_selection(["--outer-loop", "nope"])
    assert exc.value.code == 2


def test_target_inputs_default_to_none():
    from vibe_serve.cli import _build_agent_parser

    args = _build_agent_parser().parse_args([])

    assert args.input is None
    assert args.ref is None
    assert args.acc_checker is None
    assert args.bench is None
    assert args.profiler is ProfilerKind.AUTO
    assert args.domain is DomainName.LLM_SERVING


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
    args = parser.parse_args(["--input", "examples/data-structures/queue-default"])

    assert args.input == Path("examples/data-structures/queue-default")


@pytest.mark.parametrize(
    "builder_name,validator_name",
    [
        ("_build_agent_parser", "_validate_agent"),
        ("_build_evolve_parser", "_validate_evolve"),
        ("_build_openevolve_parser", "_validate_openevolve"),
        ("_build_plain_parser", "_validate_plain"),
    ],
)
def test_profiler_none_is_valid_with_modal(builder_name, validator_name):
    import vibe_serve.cli as cli

    parser = getattr(cli, builder_name)()
    validator = getattr(cli, validator_name)
    args = parser.parse_args(["--modal", "--profiler", "none", *TARGET_ARGS])

    assert args.profiler is ProfilerKind.NONE
    validator(args)


def test_agent_parser_outputs_domain_enum():
    from vibe_serve.cli import _build_agent_parser

    args = _build_agent_parser().parse_args(["--domain", "generic"])

    assert args.domain is DomainName.GENERIC


@pytest.mark.parametrize(
    "argv",
    [
        ["--profiler", "bogus"],
        ["--domain", "bogus"],
    ],
)
def test_agent_parser_rejects_invalid_enum_args(argv):
    from vibe_serve.cli import _build_agent_parser

    with pytest.raises(SystemExit) as exc:
        _build_agent_parser().parse_args(argv)

    assert exc.value.code == 2


def test_validate_target_inputs_derives_bundle_paths_from_input(tmp_path):
    from vibe_serve.cli import _build_agent_parser

    bundle = tmp_path / "queue-default"
    (bundle / "reference").mkdir(parents=True)
    (bundle / "accuracy_checker").mkdir()
    (bundle / "benchmark").mkdir()

    args = _build_agent_parser().parse_args(["--input", str(bundle)])

    _validate_target_inputs(args)

    assert args.ref == str(bundle / "reference")
    assert args.acc_checker == bundle / "accuracy_checker"
    assert args.bench == bundle / "benchmark"


def test_validate_target_inputs_keeps_explicit_overrides(tmp_path):
    from vibe_serve.cli import _build_agent_parser

    bundle = tmp_path / "queue-default"
    (bundle / "reference").mkdir(parents=True)
    (bundle / "accuracy_checker").mkdir()
    custom_bench = tmp_path / "custom-benchmark"
    custom_bench.mkdir()

    args = _build_agent_parser().parse_args(["--input", str(bundle), "--bench", str(custom_bench)])

    _validate_target_inputs(args)

    assert args.ref == str(bundle / "reference")
    assert args.acc_checker == bundle / "accuracy_checker"
    assert args.bench == custom_bench


def test_validate_target_inputs_rejects_missing_input_dir(tmp_path, capsys):
    from vibe_serve.cli import _build_agent_parser

    missing = tmp_path / "missing"
    args = _build_agent_parser().parse_args(["--input", str(missing)])

    with pytest.raises(SystemExit) as exc:
        _validate_target_inputs(args)

    assert exc.value.code == 2
    assert "--input path does not exist" in capsys.readouterr().err


def test_validate_target_inputs_reports_missing_input_subdirs(tmp_path, capsys):
    from vibe_serve.cli import _build_agent_parser

    bundle = tmp_path / "incomplete"
    (bundle / "reference").mkdir(parents=True)

    args = _build_agent_parser().parse_args(["--input", str(bundle)])

    with pytest.raises(SystemExit) as exc:
        _validate_target_inputs(args)

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "accuracy_checker/ for --acc-checker" in err
    assert "benchmark/ for --bench" in err


def test_validate_target_inputs_requires_bundle_paths(capsys):
    from vibe_serve.cli import _build_agent_parser

    args = _build_agent_parser().parse_args(["--ref", "examples/Llama-3-8B/reference"])

    with pytest.raises(SystemExit) as exc:
        _validate_target_inputs(args)

    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--acc-checker" in err
    assert "--bench" in err


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
