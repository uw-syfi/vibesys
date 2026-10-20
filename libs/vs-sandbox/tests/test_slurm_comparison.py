"""A comparison baseline's server replaces the candidate's in the operator's Slurm policy."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api.slurm import (
    BaselineServer,
    ComparisonMachine,
    ComparisonNotConfiguredError,
    SlurmExecutionPolicy,
    SlurmPolicyError,
    comparison_policy,
    load_slurm_policy,
    serve_command,
    serve_script,
)
from vs_slurm.api import SlurmService

if TYPE_CHECKING:
    from pathlib import Path

_DIGEST = "c" * 64
_IMAGE = f"docker://rocm/vllm@sha256:{_DIGEST}"
# Words a task or an operator might write, including the ones a shell would act on.
_WORDS = st.text(
    alphabet=st.sampled_from(list("abz09-_=./: '\"$;&|<>*`\\()")), min_size=1, max_size=14
)
_ARGV = st.lists(_WORDS, min_size=1, max_size=5).map(tuple)
_NAMES = st.from_regex(r"[A-Z_][A-Z0-9_]{0,8}", fullmatch=True)
_ENV = st.dictionaries(_NAMES, _WORDS, max_size=3)
_MACHINES = st.builds(
    ComparisonMachine,
    gpu=st.sampled_from(["rocm", "nvidia"]),
    image_cache_dir=st.just("/cluster/images"),
    model_cache_dir=st.just("/cluster/hf"),
    binds=st.lists(_WORDS, max_size=2).map(tuple),
    env_passthrough=st.lists(_NAMES, max_size=2, unique=True).map(tuple),
)


def _server(argv: tuple[str, ...], env: dict[str, str] | None = None) -> BaselineServer:
    return BaselineServer(
        image=_IMAGE,
        argv=(*argv, "--port", "${PORT}"),
        env=env or {},
        ready_timeout_seconds=900,
        benchmark_args=("--backend", "vllm"),
    )


def _machine(**changes: object) -> ComparisonMachine:
    fields: dict[str, object] = {
        "gpu": "rocm",
        "image_cache_dir": "/cluster/images",
        "model_cache_dir": "/cluster/hf",
    } | changes
    return ComparisonMachine.model_validate(fields)


def _exec_tokens(script: str) -> list[str]:
    """The words of the script's last line, as a POSIX shell reads them (``$1`` stays ``$1``)."""
    return shlex.split(script.splitlines()[-1])


@given(machine=_MACHINES, argv=_ARGV, env=_ENV)
def test_the_engine_is_exec_ed_with_its_argv_whatever_the_words_hold(
    machine: ComparisonMachine, argv: tuple[str, ...], env: dict[str, str]
) -> None:
    server = _server(argv, env)

    tokens = _exec_tokens(serve_script(machine, server))

    expected = [part.replace("${PORT}", "$1") for part in server.argv]
    assert tokens[-len(expected) :] == expected
    assert tokens[: tokens.index("--cleanenv")] == ["exec", machine.apptainer, "exec"]
    assert f"/cluster/images/{_DIGEST}.sif" in tokens


@given(machine=_MACHINES, argv=_ARGV, env=_ENV)
def test_the_engine_environment_is_exported_whole_and_quoted(
    machine: ComparisonMachine, argv: tuple[str, ...], env: dict[str, str]
) -> None:
    script = serve_script(machine, _server(argv, env))

    words = [
        shlex.split(line) for line in script.splitlines() if line.startswith("export APPTAINERENV_")
    ]
    exported = {
        assignment.partition("=")[0].removeprefix("APPTAINERENV_"): assignment.partition("=")[2]
        for _, assignment in (row for row in words if len(row) == 2)
    }

    assert all(exported[name] == value for name, value in env.items())


@given(machine=_MACHINES, argv=_ARGV)
def test_the_model_cache_is_mounted_read_only_and_the_gpu_runtime_is_bound(
    machine: ComparisonMachine, argv: tuple[str, ...]
) -> None:
    tokens = _exec_tokens(serve_script(machine, _server(argv)))

    assert "/cluster/hf:/cluster/hf:ro" in tokens
    assert ("--rocm" if machine.gpu == "rocm" else "--nv") in tokens
    assert ("--nv" if machine.gpu == "rocm" else "--rocm") not in tokens


@given(machine=_MACHINES, argv=_ARGV)
def test_the_image_is_pulled_once_under_a_lock_and_cached_by_digest(
    machine: ComparisonMachine, argv: tuple[str, ...]
) -> None:
    script = serve_script(machine, _server(argv))

    assert script.count(f"{machine.apptainer} pull") == 1
    assert "flock 9" in script
    assert script.index("flock 9") < script.index(f"{machine.apptainer} pull")
    assert _IMAGE in shlex.split(next(ln for ln in script.splitlines() if " pull " in ln))


@given(machine=_MACHINES, argv=_ARGV)
def test_the_service_command_takes_the_jobs_port_as_one_complete_item(
    machine: ComparisonMachine, argv: tuple[str, ...]
) -> None:
    command = serve_command(machine, _server(argv))

    assert command[:2] == ("sh", "-c")
    assert command[3:] == ("vs-baseline", "VIBESYS_DYNAMIC_PORT")
    assert "VIBESYS_DYNAMIC_PORT" not in command[2]


def test_the_candidates_server_gives_way_but_the_rest_of_the_policy_stays() -> None:
    candidate = SlurmExecutionPolicy(
        service=SlurmService(
            command=("python", "-m", "engine.server", "VIBESYS_DYNAMIC_PORT"),
            readiness_url="http://127.0.0.1:VIBESYS_DYNAMIC_PORT/v1/models",
            startup_timeout_seconds=900,
        ),
        setup_script="/cluster/setup.sh",
        accuracy_arguments=("--base-url", "http://127.0.0.1:VIBESYS_DYNAMIC_PORT"),
        benchmark_arguments=("--base-url", "http://127.0.0.1:VIBESYS_DYNAMIC_PORT/v1"),
    )

    policy = comparison_policy(candidate, _machine(), _server(("vllm", "serve")))

    assert policy.service is not None
    assert "engine.server" not in " ".join(policy.service.command)
    assert candidate.service is not None
    assert policy.service.readiness_url == candidate.service.readiness_url
    assert policy.service.startup_timeout_seconds == 900
    assert policy.setup_script == candidate.setup_script
    assert policy.ledger is None
    assert policy.accuracy_arguments == candidate.accuracy_arguments
    assert policy.benchmark_arguments == (*candidate.benchmark_arguments, "--backend", "vllm")
    assert candidate.service.command[2] == "engine.server"


def test_a_policy_without_a_service_cannot_measure_a_comparison() -> None:
    with pytest.raises(ComparisonNotConfiguredError, match=r"vibesys\.service"):
        comparison_policy(SlurmExecutionPolicy(), _machine(), _server(("vllm", "serve")))


_VALID = {
    "gpu": '"rocm"',
    "image_cache_dir": '"/cluster/images"',
    "model_cache_dir": '"/cluster/hf"',
    "binds": '["/cluster/lib:/opt/lib:ro"]',
    "env_passthrough": '["HF_TOKEN"]',
}


def _operator_file(tmp_path: Path, table: dict[str, str] | None) -> Path:
    body = (
        ""
        if table is None
        else "[vibesys.comparison]\n"
        + "".join(f"{key} = {value}\n" for key, value in table.items())
    )
    path = tmp_path / "slurm.toml"
    path.write_text(f'[slurm]\nname = "cluster"\n[vibesys]\n{body}', encoding="utf-8")
    return path


def test_the_machine_table_is_read_from_the_operator_file(tmp_path: Path) -> None:
    policy = load_slurm_policy(_operator_file(tmp_path, _VALID))

    assert policy.comparison is not None
    assert policy.comparison.model_cache_dir == "/cluster/hf"
    assert policy.comparison.binds == ("/cluster/lib:/opt/lib:ro",)
    assert policy.comparison.env_passthrough == ("HF_TOKEN",)


def test_a_machine_without_the_table_has_no_comparison_settings(tmp_path: Path) -> None:
    assert load_slurm_policy(_operator_file(tmp_path, None)).comparison is None


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("secret", "1"),
        ("image_cache_dir", '"relative/images"'),
        ("model_cache_dir", '"/a/../b"'),
        ("gpu", '"tpu"'),
        ("env_passthrough", '["not a name"]'),
    ],
)
def test_a_bad_machine_table_is_rejected_naming_the_key(
    tmp_path: Path, key: str, value: str
) -> None:
    table = {**_VALID, key: value}

    with pytest.raises(SlurmPolicyError, match=rf"vibesys\.comparison\.{key}"):
        load_slurm_policy(_operator_file(tmp_path, table))
