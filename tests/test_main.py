"""Tests for the Python backend entry point."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from vibesys.domains.base import DomainName
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.main import (
    _control_socket_from_argv,
    _detect_resume_round,
    _extract_flag,
    _extract_loop_selection,
    _infer_resume_input,
    _load_objectives_toml,
    _parse_cli_objective,
    _prepare_stub_agent_smoke_defaults,
    _prune_rounds_state,
    _render_configuration_error,
    _resolve_run_dir,
    _validate_target_inputs,
    load_config_and_skills,
    main,
    parse_cli_invocation,
)
from vibesys.profilers import ProfilerKind
from vs_github import GitHubCLI


def _patch_loop_runner(loop_name: str, runner: Mock):
    """Swap the dispatch entry's ``run`` function for *runner*.

    ``_LOOP_COMMANDS`` holds direct function references, so patching the
    module-level function name no longer affects dispatch — patch the
    command record instead.
    """
    import dataclasses

    import vibesys.main as cli

    command = cli._LOOP_COMMANDS[loop_name]
    patched = dataclasses.replace(command, run=runner)
    return patch.dict(cli._LOOP_COMMANDS, {loop_name: patched})


TARGET_ARGS = ["--input", "examples/model-serving/Llama-3-8B"]


def _write_input_bundle(tmp_path: Path) -> Path:
    bundle = tmp_path / "queue-spsc"
    (bundle / "reference").mkdir(parents=True)
    (bundle / "OBJECTIVE.md").write_text("objective\n")
    (bundle / "vibesys.input.toml").write_text(
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


def _write_resume_event(
    exp_dir: Path, input_path: Path, *, event_type: str = "run_started"
) -> None:
    logs = exp_dir / "logs"
    logs.mkdir(parents=True)
    content = (
        '{"type": "server_started", "data": null}\n'
        f'{{"type": "{event_type}", "data": '
        f'{{"kind": "run_started", "outer_loop": "agent", "input": "{input_path}", '
        '"max_rounds": 2}}\n'
    )
    (logs / "run-events.jsonl").write_text(content)


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
    from vibesys.main import _build_agent_parser

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
    from vibesys.main import _build_agent_parser

    with pytest.raises(ConfigurationError, match="unrecognized arguments"):
        _build_agent_parser().parse_args([obsolete_flag, "support"])


@pytest.mark.parametrize(
    "builder_name",
    [
        "_build_agent_parser",
        "_build_evolve_parser",
        "_build_plain_parser",
    ],
)
def test_input_arg_is_available_on_all_loop_parsers(builder_name):
    import vibesys.main as cli

    parser = getattr(cli, builder_name)()
    args = parser.parse_args(["--input", "examples/data-structures/queue-spsc"])

    assert args.input == Path("examples/data-structures/queue-spsc")


def test_remote_repository_options_are_user_configurable():
    from vibesys.main import _build_agent_parser
    from vibesys.run import RepositoryVisibility

    args = _build_agent_parser().parse_args(
        ["--repo", "my-lab/trial", "--repo-visibility", "internal"]
    )

    assert args.repo == "my-lab/trial"
    assert args.repo_visibility is RepositoryVisibility.INTERNAL


def test_short_repository_name_uses_configured_owner(tmp_path):
    from vibesys.main import _build_agent_parser
    from vibesys.run import RepositoryVisibility

    config_path = tmp_path / "agent.toml"
    config_path.write_text(
        """\
[model]
name = "gpt-5.5"

[repository]
owner = "my-playground"
visibility = "internal"
"""
    )
    args = _build_agent_parser().parse_args(
        ["--repo", "generated-trial", "--config", str(config_path), "--no-skills"]
    )

    load_config_and_skills(args)

    assert args.repo == "my-playground/generated-trial"
    assert args.repo_visibility is RepositoryVisibility.INTERNAL


@pytest.mark.parametrize(
    "builder_name,validator_name",
    [
        ("_build_agent_parser", "_validate_agent"),
        ("_build_evolve_parser", "_validate_evolve"),
        ("_build_plain_parser", "_validate_plain"),
    ],
)
def test_profiler_none_is_valid_with_modal(builder_name, validator_name, tmp_path):
    import vibesys.main as cli

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
    from vibesys.main import _build_agent_parser

    with pytest.raises(ConfigurationError) as exc:
        _build_agent_parser().parse_args(argv)

    assert exc.value.diagnostic.code == "invalid_arguments"


def test_agent_parser_rejects_obsolete_target_flags():
    from vibesys.main import _build_agent_parser

    with pytest.raises(ConfigurationError):
        _build_agent_parser().parse_args(["--ref", "examples/Llama-3-8B/reference"])


def test_validate_target_inputs_loads_manifest(tmp_path):
    from vibesys.main import _build_agent_parser

    bundle = _write_input_bundle(tmp_path)
    args = _build_agent_parser().parse_args(["--input", str(bundle)])

    _validate_target_inputs(args)

    assert args.input_bundle.root == bundle.resolve()
    assert args.input_bundle.domain is DomainName.GENERIC
    assert args.input_bundle.accuracy_command_display == "uv run python accuracy_checker/checker.py"
    assert args.input_bundle.benchmark_command_display == "uv run python benchmark/benchmark.py"


def test_agent_parser_rejects_domain_override_flag():
    from vibesys.main import _build_agent_parser

    with pytest.raises(ConfigurationError):
        _build_agent_parser().parse_args(["--domain", "llm-serving"])


def test_validate_target_inputs_loads_trusted_benchmark_result_contract(tmp_path):
    from vibesys.main import _build_agent_parser

    bundle = _write_input_bundle(tmp_path)
    manifest = bundle / "vibesys.input.toml"
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
    from vibesys.main import _build_agent_parser

    missing = tmp_path / "missing"
    args = _build_agent_parser().parse_args(["--input", str(missing)])

    with pytest.raises(ConfigurationError) as exc:
        _validate_target_inputs(args)

    assert "--input path does not exist" in exc.value.diagnostic.message


def test_validate_target_inputs_reports_missing_manifest(tmp_path):
    from vibesys.main import _build_agent_parser

    bundle = tmp_path / "incomplete"
    bundle.mkdir()
    (bundle / "OBJECTIVE.md").write_text("objective\n")

    args = _build_agent_parser().parse_args(["--input", str(bundle)])

    with pytest.raises(ConfigurationError) as exc:
        _validate_target_inputs(args)

    assert "vibesys.input.toml" in exc.value.diagnostic.message


def test_validate_target_inputs_reports_missing_command(tmp_path):
    from vibesys.main import _build_agent_parser

    bundle = _write_input_bundle(tmp_path)
    tools = bundle / "tools"
    tools.mkdir()
    (tools / "check").write_text("#!/usr/bin/env bash\nexit 0\n")
    (tools / "check").chmod(0o755)
    (bundle / "vibesys.input.toml").write_text(
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
    from vibesys.main import _build_agent_parser

    args = _build_agent_parser().parse_args([])

    with pytest.raises(ConfigurationError) as exc:
        _validate_target_inputs(args)

    assert "missing required target input: --input" in exc.value.diagnostic.message


def test_stub_agent_smoke_defaults_supply_input_and_unique_exp_name():
    argv = _prepare_stub_agent_smoke_defaults(["--stub-agent", "--max-rounds", "1"])

    assert argv[:2] == ["--input", str(Path("examples/data-structures/queue-spsc").resolve())]
    assert argv[2] == "--exp-name"
    assert argv[3].startswith("stub-smoke-")
    assert argv[-2:] == ["--max-rounds", "1"]


def test_stub_agent_smoke_defaults_preserve_explicit_input():
    argv = ["--stub-agent", "--input", "examples/kv-store"]

    assert _prepare_stub_agent_smoke_defaults(argv) == argv


def test_stub_agent_can_run_without_agent_toml(tmp_path):
    from vibesys.main import _build_agent_parser

    bundle = _write_input_bundle(tmp_path)
    args = _build_agent_parser().parse_args(
        [
            "--stub-agent",
            "--input",
            str(bundle),
            "--config",
            str(tmp_path / "missing-agent.toml"),
            "--no-skills",
        ]
    )
    _validate_target_inputs(args)

    config, skills, _ = load_config_and_skills(args, domain=DomainName.GENERIC)

    assert config.model.name == "gpt-5.5"
    assert skills is None


def test_missing_config_reports_configuration_error(tmp_path):
    from vibesys.main import _build_agent_parser

    bundle = _write_input_bundle(tmp_path)
    args = _build_agent_parser().parse_args(
        [
            "--input",
            str(bundle),
            "--config",
            str(tmp_path / "missing-agent.toml"),
        ]
    )

    with pytest.raises(ConfigurationError) as exc:
        load_config_and_skills(args, domain=DomainName.GENERIC)

    assert exc.value.diagnostic.code == "config_load_failed"
    assert exc.value.diagnostic.stage == "config_loading"


# ---------------------------------------------------------------------------
# validate command
# ---------------------------------------------------------------------------


def test_tui_defaults_uses_repository_config_and_generated_name(tmp_path, capsys):
    import json

    config_path = tmp_path / "agent.toml"
    config_path.write_text(
        """\
[model]
name = "gpt-5.5"

[repository]
owner = "vibesys-playground"
visibility = "private"
"""
    )
    input_path = tmp_path / "Queue MPSC"
    input_path.mkdir()
    argv = [
        "vibesys",
        "tui-defaults",
        "--config",
        str(config_path),
        "--input",
        str(input_path),
    ]

    with patch.object(sys, "argv", argv):
        main()

    defaults = json.loads(capsys.readouterr().out)
    assert defaults["repository_owner"] == "vibesys-playground"
    assert defaults["visibility"] == "private"
    assert defaults["experiment_name"].startswith("queue-mpsc-")
    assert defaults["repository_name"] == defaults["experiment_name"]


def test_validate_command_defaults_to_current_input_bundle(monkeypatch, tmp_path, capsys):
    bundle = _write_input_bundle(tmp_path)
    monkeypatch.chdir(bundle)
    monkeypatch.setattr(sys, "argv", ["vibesys", "validate"])

    main()

    output = capsys.readouterr().out
    assert "VibeSys validation passed" in output
    assert f"input bundle: {bundle}" in output
    assert "accuracy command: uv run python accuracy_checker/checker.py" in output
    assert "benchmark command: uv run python benchmark/benchmark.py" in output


def test_validate_command_accepts_input_bundle_path(tmp_path, capsys):
    bundle = _write_input_bundle(tmp_path)
    argv = ["vibesys", "validate", str(bundle)]

    with patch.object(sys, "argv", argv):
        main()

    output = capsys.readouterr().out
    assert "input bundle is valid" in output
    assert f"objective: {bundle / 'OBJECTIVE.md'}" in output


def test_validate_command_rejects_run_input_flag(tmp_path, capsys):
    bundle = _write_input_bundle(tmp_path)
    argv = ["vibesys", "validate", "--input", str(bundle)]

    with patch.object(sys, "argv", argv), pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 2
    assert "unrecognized arguments" in capsys.readouterr().err


def test_all_example_input_bundles_pass_validate(example_input_bundles: tuple[Path, ...], capsys):
    for input_bundle in example_input_bundles:
        argv = ["vibesys", "validate", str(input_bundle)]
        with patch.object(sys, "argv", argv):
            main()

    output = capsys.readouterr().out
    assert output.count("VibeSys validation passed") == len(example_input_bundles)


def test_validate_command_reports_invalid_harness_without_running_agent(tmp_path, capsys):
    bundle = _write_input_bundle(tmp_path)
    (bundle / "OBJECTIVE.md").unlink()
    argv = ["vibesys", "validate", str(bundle)]

    runner = Mock()
    with (
        patch.object(sys, "argv", argv),
        _patch_loop_runner("agent", runner),
        pytest.raises(SystemExit) as exc,
    ):
        main()

    assert exc.value.code == 1
    runner.assert_not_called()
    error = capsys.readouterr().err
    assert "Validation failed for input bundle" in error
    assert "OBJECTIVE.md not found" in error


def test_resume_without_input_infers_original_input(monkeypatch, tmp_path):
    import vibesys.main as cli

    bundle = _write_input_bundle(tmp_path)
    run_dir = tmp_path / "exp_env" / "20260716-180256-test"
    _write_resume_event(run_dir, bundle)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)

    invocation = parse_cli_invocation(["--outer-loop", "agent", "--resume", "20260716-180256-test"])

    assert invocation.args.resume == "20260716-180256-test"
    assert invocation.args.input == bundle
    assert invocation.args.input_bundle.root == bundle.resolve()


def test_resume_accepts_exp_env_path_without_input(monkeypatch, tmp_path):
    import vibesys.main as cli

    bundle = _write_input_bundle(tmp_path)
    run_dir = tmp_path / "exp_env" / "20260716-180256-test"
    _write_resume_event(run_dir, bundle)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)

    invocation = parse_cli_invocation(
        ["--outer-loop", "agent", "--resume", str(run_dir.relative_to(tmp_path))]
    )

    assert invocation.args.resume == "20260716-180256-test"
    assert invocation.args.input_bundle.root == bundle.resolve()


def test_resume_accepts_external_local_clone_and_materialized_input(monkeypatch, tmp_path):
    import vibesys.main as cli

    run_dir = tmp_path / "clone"
    workspace = _write_input_bundle(run_dir)
    # These source locations are intentionally unavailable in a clone. They
    # were already copied into the workspace by the original fresh run.
    manifest = workspace / "vibesys.input.toml"
    manifest.write_text(
        manifest.read_text()
        + '\n[workspace]\nseed = "../../starters/missing"\n'
        + '\n[evaluator]\nsource = "../../evaluators/missing"\n'
    )
    experiment = run_dir / "experiment"
    experiment.mkdir()
    workspace.rename(experiment / "workspace")
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path / "project")

    invocation = parse_cli_invocation(["--outer-loop", "agent", "--resume", str(experiment)])

    assert invocation.args.resume == str(experiment.resolve())
    assert invocation.args.input_bundle.root == (experiment / "workspace").resolve()
    assert invocation.args.input_bundle.workspace_seed_path is None
    assert invocation.args.input_bundle.evaluator_path is None


def test_resume_github_repo_clones_into_exp_env(monkeypatch, tmp_path):
    import vibesys.main as cli

    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        commands.append(command)
        if command[:3] == ["gh", "repo", "clone"]:
            Path(command[-1]).mkdir(parents=True)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(cli, "GitHubCLI", lambda: GitHubCLI(_runner=fake_run))

    assert _resolve_run_dir("vibesys-playground/trial") == "trial"
    assert commands == [
        ["gh", "auth", "status", "--hostname", "github.com"],
        ["gh", "repo", "clone", "vibesys-playground/trial", str(tmp_path / "exp_env/trial")],
    ]


def test_resume_github_repo_reuses_matching_local_clone(monkeypatch, tmp_path):
    import vibesys.main as cli

    destination = tmp_path / "exp_env" / "trial"
    destination.mkdir(parents=True)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)

    def fake_run(command, **_kwargs):
        assert command == ["git", "remote", "get-url", "origin"]
        return subprocess.CompletedProcess(
            command,
            0,
            "git@github.com:vibesys-playground/trial.git\n",
            "",
        )

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    monkeypatch.setattr(
        cli,
        "GitHubCLI",
        lambda: pytest.fail("matching local clone should not invoke GitHub CLI"),
    )

    assert _resolve_run_dir("vibesys-playground/trial") == "trial"


def test_resume_github_repo_explains_missing_authentication(monkeypatch, tmp_path):
    import vibesys.main as cli

    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)

    def fake_run(command, **_kwargs):
        return subprocess.CompletedProcess(command, 1, "", "not logged into any GitHub hosts")

    monkeypatch.setattr(cli, "GitHubCLI", lambda: GitHubCLI(_runner=fake_run))

    with pytest.raises(ConfigurationError) as exc:
        _resolve_run_dir("vibesys-playground/trial")

    assert exc.value.diagnostic.code == "resume_clone_failed"
    assert "gh auth login --hostname github.com" in exc.value.diagnostic.message
    assert "not logged into any GitHub hosts" in exc.value.diagnostic.message


def test_resume_rejects_creating_a_second_repository(tmp_path):
    bundle = _write_input_bundle(tmp_path)

    with pytest.raises(ConfigurationError) as exc:
        parse_cli_invocation(
            [
                "--outer-loop",
                "agent",
                "--input",
                str(bundle),
                "--resume",
                "run",
                "--repo",
                "my-lab/run",
            ]
        )

    assert exc.value.diagnostic.code == "invalid_arguments"


def test_resume_latest_without_input_uses_latest_run_metadata(monkeypatch, tmp_path):
    import vibesys.main as cli

    older_bundle = _write_input_bundle(tmp_path / "older")
    latest_bundle = _write_input_bundle(tmp_path / "latest")
    older = tmp_path / "exp_env" / "20260716-100000-test"
    latest = tmp_path / "exp_env" / "20260716-180256-test"
    _write_resume_event(older, older_bundle)
    _write_resume_event(latest, latest_bundle)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)

    invocation = parse_cli_invocation(["--outer-loop", "agent", "--resume"])

    assert invocation.args.resume == "20260716-180256-test"
    assert invocation.args.input_bundle.root == latest_bundle.resolve()


def test_resume_latest_reports_missing_exp_env(monkeypatch, tmp_path):
    import vibesys.main as cli

    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)

    with pytest.raises(ConfigurationError) as exc:
        _resolve_run_dir("latest")

    assert exc.value.diagnostic.code == "resume_not_found"


def test_resume_latest_reports_empty_exp_env(monkeypatch, tmp_path):
    import vibesys.main as cli

    (tmp_path / "exp_env").mkdir()
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)

    with pytest.raises(ConfigurationError) as exc:
        _resolve_run_dir("latest")

    assert exc.value.diagnostic.code == "resume_not_found"


def test_resume_without_input_reports_missing_metadata(monkeypatch, tmp_path):
    import vibesys.main as cli

    (tmp_path / "exp_env" / "20260716-180256-test" / "logs").mkdir(parents=True)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)

    with pytest.raises(ConfigurationError) as exc:
        parse_cli_invocation(["--outer-loop", "agent", "--resume", "20260716-180256-test"])

    assert exc.value.diagnostic.code == "resume_input_not_found"
    assert exc.value.diagnostic.stage == "resume_resolution"


def test_resume_input_ignores_blank_and_non_run_events(tmp_path):
    bundle = _write_input_bundle(tmp_path)
    run_dir = tmp_path / "exp_env" / "run"
    logs = run_dir / "logs"
    logs.mkdir(parents=True)
    (logs / "run-events.jsonl").write_text(
        "\n"
        '{"type": "server_started", "data": null}\n'
        f'{{"type": "run_started", "data": {{"input": "{bundle}"}}}}\n'
    )

    assert _infer_resume_input(run_dir) == bundle


def test_resume_input_reports_invalid_json(tmp_path):
    run_dir = tmp_path / "exp_env" / "run"
    logs = run_dir / "logs"
    logs.mkdir(parents=True)
    (logs / "run-events.jsonl").write_text("{not-json}\n")

    with pytest.raises(ConfigurationError) as exc:
        _infer_resume_input(run_dir)

    assert exc.value.diagnostic.code == "resume_input_invalid"


@pytest.mark.parametrize(
    "event",
    [
        {"type": "run_started", "data": None},
        {"type": "run_started", "data": {}},
    ],
)
def test_resume_input_reports_missing_input_in_run_started(tmp_path, event):
    import json

    run_dir = tmp_path / "exp_env" / "run"
    logs = run_dir / "logs"
    logs.mkdir(parents=True)
    (logs / "run-events.jsonl").write_text(json.dumps(event) + "\n")

    with pytest.raises(ConfigurationError) as exc:
        _infer_resume_input(run_dir)

    assert exc.value.diagnostic.code == "resume_input_not_found"


def test_resume_round_defaults_when_rounds_json_missing_or_invalid(tmp_path):
    exp_dir = tmp_path / "run"

    assert _detect_resume_round(exp_dir) == 1

    logs = exp_dir / "logs"
    logs.mkdir(parents=True)
    (logs / "rounds.json").write_text("not-json")

    assert _detect_resume_round(exp_dir) == 1


def test_resume_round_counts_round_entries_and_prunes_later_rounds(tmp_path):
    rounds_json = tmp_path / "run" / "logs" / "rounds.json"
    rounds_json.parent.mkdir(parents=True)
    rounds_json.write_text('[{"round": 1}, {"round": 2}, {"round": 3}]')

    assert _detect_resume_round(tmp_path / "run") == 4

    _prune_rounds_state(tmp_path / "run", keep_up_to=3)

    assert rounds_json.read_text() == '[\n  {\n    "round": 1\n  },\n  {\n    "round": 2\n  }\n]'


@pytest.mark.parametrize(
    "spec,message",
    [
        ("latency", "must be 'name:max' or 'name:min'"),
        (":max", "metric name is empty"),
        ("latency:avg", "direction must be 'max' or 'min'"),
    ],
)
def test_parse_cli_objective_rejects_malformed_specs(spec, message):
    with pytest.raises(Exception) as exc:
        _parse_cli_objective(spec)

    assert message in str(exc.value)


def test_load_objectives_toml_reports_malformed_entries(tmp_path):
    (tmp_path / "objectives.toml").write_text(
        """
[[objective]]
name = "latency"
direction = "avg"
""".lstrip()
    )

    with pytest.raises(ValueError, match="Malformed entry"):
        _load_objectives_toml(tmp_path)


def test_control_socket_from_argv_handles_empty_equals_and_space_form():
    assert _control_socket_from_argv(["--control-socket="]) is None
    assert _control_socket_from_argv(["--control-socket", "/tmp/vs.sock"]) == Path("/tmp/vs.sock")


def test_render_configuration_error_prints_usage(capsys):
    diagnostic = ConfigurationError(
        diagnostic=ConfigurationDiagnostic(
            code="invalid_arguments",
            stage="argument_parsing",
            message="bad args",
            usage="usage: vibesys ...",
        )
    )

    with pytest.raises(SystemExit) as exc:
        _render_configuration_error(diagnostic)

    assert exc.value.code == 2
    assert "bad args" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# main() routes to the right runner
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("loop_name", ["agent", "evolve", "plain"])
def test_main_routes_to_runner(loop_name: str):
    argv = ["vibesys", "--outer-loop", loop_name, "--exp-name", "x", *TARGET_ARGS]
    runner = Mock()
    with patch.object(sys, "argv", argv), _patch_loop_runner(loop_name, runner):
        main()
        runner.assert_called_once()
        args = runner.call_args.args[0]
        assert args.exp_name == "x"
        assert args.input_bundle.root.name == "Llama-3-8B"


def test_main_tty_run_stays_in_python_cli():
    argv = [
        "vibesys",
        "--outer-loop",
        "agent",
        "--exp-name",
        "x",
        *TARGET_ARGS,
    ]
    runner = Mock()
    with (
        patch.object(sys, "argv", argv),
        patch.object(sys.stdin, "isatty", return_value=True),
        patch.object(sys.stdout, "isatty", return_value=True),
        _patch_loop_runner("agent", runner),
    ):
        main()

    runner.assert_called_once()


def test_main_headless_skips_tui():
    argv = [
        "vibesys",
        "--outer-loop",
        "agent",
        "--headless",
        *TARGET_ARGS,
    ]
    runner = Mock()
    with (
        patch.object(sys, "argv", argv),
        _patch_loop_runner("agent", runner),
    ):
        main()
    runner.assert_called_once()
