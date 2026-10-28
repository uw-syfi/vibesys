"""``machine.describe`` and ``checkout.inspect``: a machine's facts and what a path holds."""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.server.host_helper_support import (
    VIBESYS_VERSION,
    FakeAgentLogins,
    FakeCheckout,
    fake_host,
    quiet_machine,
    record,
)

from server.host_agent_logins import AgentLogin
from server.host_api import (
    Checkout,
    CheckoutFound,
    CheckoutInspectReply,
    CheckoutInspectRequest,
    CheckoutNotFound,
    CheckoutTooOld,
    InstanceList,
    MachineDescribeReply,
    MachineDescribeRequest,
    RunPhase,
    RunStatus,
)
from server.host_compatibility_generated import MIN_CHECKOUT_VERSION
from server.host_helper_machine import (
    DOCKER_VERSION_COMMAND,
    HOST_GPU_QUERIES,
    HostMachineProbe,
    release_of,
)
from server.host_helper_requests import answer, run_counts
from server.host_machine_facts import (
    Accelerator,
    DockerReachable,
    DockerUnreachable,
    Gpu,
    Hardware,
    LoginState,
    MachineCheckout,
    RunCounts,
)
from server.registry import FakeInstanceStore
from vs_sim.api import ProbeResult
from vs_sim.api.testing import ScriptedProbe, SimNetwork, SimThreads

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    type _Script = Callable[[tuple[str, ...]], ProbeResult | None]

_NVIDIA, _ROCM, _APPLE = (query.argv for query in HOST_GPU_QUERIES)
_ROCM_CSV = (
    "device,Card Series,Card Model,Card Vendor\n"
    "card0,AMD Instinct MI210,0x740f,Advanced Micro Devices\n"
    "card1,AMD Instinct MI210,0x740f,Advanced Micro Devices\n"
)


def _answers(answers: dict[tuple[str, ...], ProbeResult]) -> _Script:
    return answers.get


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        (
            {_NVIDIA: ProbeResult(0, "NVIDIA H100 80GB HBM3\nNVIDIA H100 80GB HBM3\n")},
            Hardware(
                cpu_cores=4,
                gpus=(Gpu(vendor="nvidia", model="NVIDIA H100 80GB HBM3"),) * 2,
                accelerator=Accelerator.CUDA,
            ),
        ),
        (
            {
                _NVIDIA: ProbeResult(9, "", "NVIDIA-SMI has failed"),
                _ROCM: ProbeResult(0, _ROCM_CSV),
            },
            Hardware(
                cpu_cores=4,
                gpus=(Gpu(vendor="amd", model="AMD Instinct MI210"),) * 2,
                accelerator=Accelerator.ROCM,
            ),
        ),
        (
            {_APPLE: ProbeResult(0, "Apple M3 Max\n")},
            Hardware(
                cpu_cores=4,
                gpus=(Gpu(vendor="apple", model="Apple M3 Max"),),
                accelerator=Accelerator.METAL,
            ),
        ),
        (
            {_APPLE: ProbeResult(0, "Intel(R) Core(TM) i9-9980HK\n")},
            Hardware(cpu_cores=4, gpus=(), accelerator=Accelerator.CPU),
        ),
        ({}, Hardware(cpu_cores=4, gpus=(), accelerator=Accelerator.CPU)),
    ],
    ids=["nvidia", "amd-after-a-failing-nvidia-smi", "apple-silicon", "intel-mac", "none"],
)
def test_the_first_vendor_tool_that_lists_a_gpu_names_the_hardware(
    answers: dict[tuple[str, ...], ProbeResult], expected: Hardware
) -> None:
    assert quiet_machine(_answers(answers)).hardware() == expected


def test_the_accelerator_is_cpu_exactly_when_no_gpu_is_listed() -> None:
    with pytest.raises(ValidationError, match="accelerator"):
        Hardware(cpu_cores=1, gpus=(), accelerator=Accelerator.CUDA)
    with pytest.raises(ValidationError, match="accelerator"):
        Hardware(cpu_cores=1, gpus=(Gpu(vendor="x", model="y"),), accelerator=Accelerator.CPU)


@pytest.mark.parametrize(
    ("answer_", "expected"),
    [
        (ProbeResult(0, "27.3.1\n"), DockerReachable(version="27.3.1")),
        (
            ProbeResult(
                1, "", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock\n"
            ),
            DockerUnreachable(
                reason="Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
            ),
        ),
        (ProbeResult(3, ""), DockerUnreachable(reason="docker version exited with status 3")),
        (None, DockerUnreachable(reason="docker is not installed or did not answer within 5 s")),
    ],
    ids=["reachable", "daemon-down", "silent-failure", "missing"],
)
def test_docker_is_reachable_with_its_version_or_says_why_not(
    answer_: ProbeResult | None, expected: DockerReachable | DockerUnreachable
) -> None:
    machine = quiet_machine(lambda argv: answer_ if argv == DOCKER_VERSION_COMMAND else None)
    assert machine.docker() == expected


def test_only_installed_harness_clis_are_listed_in_offer_order() -> None:
    assert quiet_machine(installed=frozenset({"codex"})).harness_clis() == ("codex",)
    assert quiet_machine(installed=frozenset({"codex", "claude"})).harness_clis() == (
        "claude",
        "codex",
    )


_HOME_BIN = "/home/user/.local/bin"
_PROGRAMS = ("claude", "codex")


@given(
    in_usr_bin=st.sets(st.sampled_from(_PROGRAMS)),
    in_home_bin=st.sets(st.sampled_from(_PROGRAMS)),
    launch_sees_home=st.booleans(),
)
def test_a_harness_cli_is_installed_when_the_path_a_run_launches_with_holds_it(
    in_usr_bin: set[str], in_home_bin: set[str], *, launch_sees_home: bool
) -> None:
    """Regression: ``~/.local/bin`` is on a login shell's ``PATH``, not on a non-login SSH one.

    The helper's own ``PATH`` never decides; the run's launch environment does, and a
    run launches provider CLIs with the interactive login shell's.
    """
    machine = quiet_machine(
        installed=frozenset(in_usr_bin),
        installed_in={_HOME_BIN: frozenset(in_home_bin)},
        launch_path=f"/usr/bin:{_HOME_BIN}" if launch_sees_home else "/usr/bin",
    )

    visible = in_usr_bin | (in_home_bin if launch_sees_home else set())
    assert machine.harness_clis() == tuple(program for program in _PROGRAMS if program in visible)


def test_the_real_program_lookup_finds_a_cli_installed_outside_the_helpers_own_path(
    tmp_path: Path,
) -> None:
    home_bin = tmp_path / ".local" / "bin"
    home_bin.mkdir(parents=True)
    codex = home_bin / "codex"
    codex.write_text("#!/bin/sh\n")
    codex.chmod(0o755)
    machine = HostMachineProbe(
        ScriptedProbe(lambda _argv: None),
        cpu_cores=lambda: 1,
        which=lambda program, path: shutil.which(program, path=path),
        harness_binaries=lambda: (("claude", "claude"), ("codex", "codex")),
        launch_environment=lambda: {"PATH": f"{tmp_path / 'empty'}:{home_bin}"},
    )

    assert machine.harness_clis() == ("codex",)


def _checkout(root: Path, version: str | None, *, helper: bool = True) -> Path:
    root.mkdir()
    declared = "" if version is None else f'version = "{version}"\n'
    (root / "pyproject.toml").write_text(f'[project]\nname = "vibesys"\n{declared}')
    if helper:
        (root / "src" / "entrypoints").mkdir(parents=True)
        (root / "src" / "entrypoints" / "host_helper.py").write_text("")
    return root


def _git(root: Path) -> _Script:
    answers = {
        ("git", "-C", str(root.resolve()), "rev-parse", "HEAD"): ProbeResult(0, "abc123\n"),
        (
            "git",
            "-C",
            str(root.resolve()),
            "status",
            "--porcelain",
            "--untracked-files=no",
        ): ProbeResult(0, " M README.md\n"),
    }
    return _answers(answers)


def test_a_checkout_with_a_host_helper_is_found_and_compatible(tmp_path: Path) -> None:
    root = _checkout(tmp_path / "vibesys", MIN_CHECKOUT_VERSION)

    assert quiet_machine(_git(root)).inspect_checkout(root) == CheckoutFound(
        root=str(root.resolve()),
        version=MIN_CHECKOUT_VERSION,
        commit="abc123",
        dirty=True,
        compatible=True,
    )


def test_a_checkout_without_a_host_helper_is_found_but_not_compatible(tmp_path: Path) -> None:
    root = _checkout(tmp_path / "vibesys", "9.0.0", helper=False)

    inspection = quiet_machine().inspect_checkout(root)

    assert inspection == CheckoutFound(
        root=str(root.resolve()), version="9.0.0", commit=None, dirty=None, compatible=False
    )


@pytest.mark.parametrize("version", ["0.0.9", "0.1.0rc1", "", "unreleased"])
def test_a_checkout_older_than_the_minimum_is_too_old(tmp_path: Path, version: str) -> None:
    root = _checkout(tmp_path / "vibesys", version)

    assert quiet_machine().inspect_checkout(root) == CheckoutTooOld(
        version=version, min=MIN_CHECKOUT_VERSION
    )


@pytest.mark.parametrize(
    "pyproject", [None, '[project]\nname = "other"\nversion = "1.0"\n', "not toml ["]
)
def test_a_path_without_a_vibesys_pyproject_holds_no_checkout(
    tmp_path: Path, pyproject: str | None
) -> None:
    if pyproject is not None:
        (tmp_path / "pyproject.toml").write_text(pyproject)

    assert quiet_machine().inspect_checkout(tmp_path) == CheckoutNotFound()
    assert quiet_machine().inspect_checkout(tmp_path / "missing") == CheckoutNotFound()


@given(st.lists(st.integers(0, 99), min_size=1, max_size=4), st.text(max_size=6))
def test_a_release_segment_is_read_from_any_version_suffix(parts: list[int], suffix: str) -> None:
    version = ".".join(map(str, parts))
    tail = "" if not suffix or suffix[0].isdigit() or suffix[0] == "." else suffix
    assert release_of(version + tail) == tuple(parts)


_NOW = datetime(2026, 10, 15, 18, 30, tzinfo=UTC).timestamp()
_STOPPED = RunPhase(status=RunStatus.STOPPED)


@given(
    offsets=st.lists(st.none() | st.floats(0, 3 * 86400), max_size=6),
    hours=st.integers(-12, 14),
    live=st.integers(0, 3),
)
def test_runs_ended_today_are_counted_from_local_midnight(
    offsets: list[float | None], hours: int, live: int
) -> None:
    zone = timezone(timedelta(hours=hours))
    ended = tuple(
        record(
            f"{index:012x}",
            phase=_STOPPED,
            ended_at=None if offset is None else _NOW - offset,
        )
        for index, offset in enumerate(offsets)
    )
    listing = InstanceList(
        instances=tuple(record(f"{index + 50:012x}") for index in range(live)), ended=ended
    )

    counts = run_counts(listing, _NOW, zone)

    midnight = datetime.fromtimestamp(_NOW, zone).replace(hour=0, minute=0, second=0, microsecond=0)
    expected = sum(
        1 for offset in offsets if offset is None or _NOW - offset >= midnight.timestamp()
    )
    assert counts == RunCounts(live=live, ended_today=expected)


def test_machine_describe_reports_the_checkout_hardware_docker_clis_and_runs() -> None:
    threads = SimThreads()
    store = FakeInstanceStore()
    store.hold("0123456789ab").publish(record("0123456789ab"))
    checkout = Checkout(root="/src/vibesys", commit="abc", dirty=False)
    host = replace(
        fake_host(threads, SimNetwork(threads), store=store),
        checkout=FakeCheckout(checkout),
        machine=quiet_machine(
            _answers({DOCKER_VERSION_COMMAND: ProbeResult(0, "27.3.1\n")}),
            installed=frozenset({"claude"}),
        ),
    )

    reply = answer(MachineDescribeRequest(request_id="m", deadline_ms=5000), host, threads.event())

    assert isinstance(reply, MachineDescribeReply)
    assert reply.machine.checkout == MachineCheckout(
        root="/src/vibesys", version=VIBESYS_VERSION, commit="abc", dirty=False
    )
    assert reply.machine.hardware == Hardware(cpu_cores=4, gpus=(), accelerator=Accelerator.CPU)
    assert reply.machine.docker == DockerReachable(version="27.3.1")
    assert reply.machine.harness_clis == ("claude",)
    assert reply.machine.harness_cli_names == {"claude": "Claude Code"}
    assert reply.machine.runs == RunCounts(live=1, ended_today=0)


_PROVIDERS = ("claude", "codex")


@given(
    installed=st.frozensets(st.sampled_from(_PROVIDERS)),
    states=st.fixed_dictionaries(
        {provider: st.sampled_from(LoginState) for provider in _PROVIDERS}
    ),
)
def test_machine_describe_reports_the_login_of_each_installed_harness_cli(
    installed: frozenset[str], states: dict[str, LoginState]
) -> None:
    threads = SimThreads()
    logins = tuple(
        AgentLogin(
            provider=provider,
            provider_name=provider,
            installed=provider in installed,
            state=states[provider] if provider in installed else LoginState.UNKNOWN,
            can_start_login=False,
        )
        for provider in _PROVIDERS
    )
    host = replace(
        fake_host(threads, SimNetwork(threads)),
        machine=quiet_machine(installed=installed),
        logins=FakeAgentLogins(logins=logins),
    )

    reply = answer(MachineDescribeRequest(request_id="m", deadline_ms=5000), host, threads.event())

    assert isinstance(reply, MachineDescribeReply)
    assert reply.machine.harness_logins == {
        provider: states[provider] for provider in reply.machine.harness_clis
    }


def test_checkout_inspect_expands_the_path_on_the_host(tmp_path: Path) -> None:
    threads = SimThreads()
    host = fake_host(threads, SimNetwork(threads))
    request = CheckoutInspectRequest(
        request_id="c", deadline_ms=5000, path=str(tmp_path / "missing")
    )

    reply = answer(request, host, threads.event())

    assert reply == CheckoutInspectReply(request_id="c", inspection=CheckoutNotFound())


def test_an_ended_record_counts_once() -> None:
    ended = record("0123456789ab", phase=_STOPPED, ended_at=_NOW - 60)
    listing = InstanceList(instances=(), ended=(ended,))
    assert run_counts(listing, _NOW, UTC) == RunCounts(live=0, ended_today=1)
