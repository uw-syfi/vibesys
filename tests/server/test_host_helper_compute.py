"""A machine's compute settings and ``measure_on``: where a run's results are measured.

The settings are read and changed through ``MachineCompute`` over in-memory files and a scripted
command probe (the real ``ssh`` and ``sinfo`` sit behind ``CommandProbe``); requests run through
:func:`answer` over the Fakes of ``host_helper_support``.
"""

from __future__ import annotations

import json
import shlex
import tomllib
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.server.host_helper_support import (
    HOME,
    FakeConfigFiles,
    FakeRunLauncher,
    fake_compute,
    fake_host,
    record,
)

from server.host_api import (
    RUN_ENVIRONMENT_FLAG,
    SLURM_CONFIG_FLAG,
    ComputeAddClusterReply,
    ComputeAddClusterRequest,
    ComputeListReply,
    ComputeListRequest,
    ComputeRemoveClusterReply,
    ComputeRemoveClusterRequest,
    ComputeTestClusterReply,
    ComputeTestClusterRequest,
    HelperError,
    HelperErrorCode,
    HelperFeature,
    HelperReply,
    HelperRequest,
    RunPreflightReply,
    RunPreflightRequest,
    RunStartReply,
    RunStartRequest,
    parse_helper_request,
    request_features,
)
from server.host_compute import (
    ClusterHardware,
    ClusterSource,
    ClusterTestFailed,
    ClusterTestPassed,
    ClusterTestStage,
    ComputeError,
    LoginNodeReach,
    ModalStatus,
    ModalTarget,
    ModalTokenSource,
    SameMachine,
    SlurmClusterSettings,
    SlurmTarget,
    SshReach,
)
from server.host_compute_runtime import (
    COMPUTE_FILE,
    MachineCompute,
    accelerator_of_gpu_model,
    parse_sinfo,
    remote_command,
    render_toml,
)
from server.host_helper_preflight import measurement_check, measurement_fits
from server.host_helper_requests import answer, launch_arguments, run_arguments
from server.host_machine_facts import Accelerator
from server.host_preflight import (
    Backend,
    Check,
    CheckId,
    CheckState,
    ConfigureCompute,
    MeasurementTargetMisfit,
    MeasurementTargetNotConfigured,
    PreflightReport,
    SettingsValid,
)
from server.host_slurm import slurm_config_of
from vs_decode.api import Unreadable
from vs_sim.api import ProbeResult
from vs_sim.api.testing import SimNetwork, SimThreads
from vs_slurm.api import SlurmConfig

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

_CONFIG = f"{HOME}/.config/vibesys/{COMPUTE_FILE}"
_OPERATOR = f"{HOME}/.config/vibesys/slurm.toml"
_SINFO_GPU = "gpu*|4|gpu:nvidia_h100:8(S:0-1)\ncpu|20|(null)\n"


def _names() -> st.SearchStrategy[str]:
    return st.from_regex(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,20}", fullmatch=True)


def _hardware() -> st.SearchStrategy[ClusterHardware | None]:
    gpus = st.builds(
        ClusterHardware,
        accelerator=st.sampled_from([Accelerator.CUDA, Accelerator.ROCM, None]),
        gpu_models=st.lists(
            st.sampled_from(["h100", "mi300x", "a100"]), unique=True, max_size=2
        ).map(tuple),
        gpus_per_node=st.integers(1, 16),
    )
    return st.none() | st.just(ClusterHardware(accelerator=Accelerator.CPU)) | gpus


def _time_limits() -> st.SearchStrategy[str | None]:
    return st.none() | st.one_of(
        st.integers(2, 9999).map(str),
        st.tuples(st.integers(0, 99), st.integers(0, 59)).map(lambda hm: f"{hm[0]}:{hm[1]:02d}:00"),
        st.tuples(st.integers(1, 9), st.integers(0, 23)).map(lambda dh: f"{dh[0]}-{dh[1]:02d}"),
    ).filter(lambda text: text not in {"0:00:00", "00:00:00"} and not text.startswith("0:0"))


def _clusters() -> st.SearchStrategy[SlurmClusterSettings]:
    return st.builds(
        SlurmClusterSettings,
        name=_names(),
        reach=st.builds(SshReach, host=_names()) | st.just(LoginNodeReach()),
        workspace=st.lists(_names(), min_size=1, max_size=3).map(
            lambda parts: "/" + "/".join(parts)
        ),
        partition=st.none() | _names(),
        nodes=st.none() | st.integers(1, 64),
        time_limit=_time_limits(),
        sbatch_options=st.lists(
            st.sampled_from(["--account=lab", "--qos=high", "--exclusive", "--gres=gpu:2"]),
            unique=True,
            max_size=3,
        ).map(tuple),
        hardware=_hardware(),
    )


def _passing(argv: tuple[str, ...]) -> ProbeResult | None:
    """A cluster that answers ssh, ``sinfo`` and the workspace test."""
    if "sinfo" in " ".join(argv):
        return ProbeResult(returncode=0, stdout=_SINFO_GPU)
    return ProbeResult(returncode=0, stdout="")


def _failing(stage: ClusterTestStage) -> Callable[[tuple[str, ...]], ProbeResult | None]:
    def script(argv: tuple[str, ...]) -> ProbeResult | None:
        text = " ".join(argv)
        if stage is ClusterTestStage.REACH and argv[0] == "ssh" and text.endswith(" true"):
            return ProbeResult(
                returncode=255, stdout="", stderr="ssh: Could not resolve hostname\n"
            )
        if stage is ClusterTestStage.SCHEDULER and "sinfo" in text:
            return ProbeResult(returncode=1, stdout="", stderr="sinfo: error: Invalid partition\n")
        if stage is ClusterTestStage.WORKSPACE and "test -d" in text.replace("'", ""):
            return ProbeResult(returncode=1, stdout="")
        return _passing(argv)

    return script


def _ssh_cluster(**fields: object) -> SlurmClusterSettings:
    base: dict[str, object] = {
        "name": "lab",
        "reach": SshReach(host="login-a"),
        "workspace": "/scratch/vibesys",
    }
    return SlurmClusterSettings.model_validate({**base, **fields})


# --- sinfo --------------------------------------------------------------------------------


def test_sinfo_of_a_gpu_cluster_names_its_partitions_models_and_per_node_count() -> None:
    partitions, hardware = parse_sinfo(_SINFO_GPU)

    assert partitions == ("gpu", "cpu")
    assert hardware == ClusterHardware(
        accelerator=Accelerator.CUDA, gpu_models=("nvidia_h100",), gpus_per_node=8
    )


@given(text=st.text(max_size=300))
def test_reading_sinfo_never_fails_and_lists_a_cpu_cluster_exactly_without_gpus(text: str) -> None:
    partitions, hardware = parse_sinfo(text)

    assert len(set(partitions)) == len(partitions)
    assert (hardware.accelerator is Accelerator.CPU) == (hardware.gpus_per_node == 0)


@given(
    counts=st.lists(st.integers(0, 16), min_size=1, max_size=5),
    model=st.sampled_from(["h100", "a100", "mi250", "mi300x"]),
)
def test_a_node_offers_the_largest_gpu_count_any_partition_lists(
    counts: list[int], model: str
) -> None:
    rows = "".join(f"p{i}|2|gpu:{model}:{n}(S:0)\n" for i, n in enumerate(counts))

    _, hardware = parse_sinfo(rows)

    assert hardware.gpus_per_node == max(counts)
    assert (hardware.gpu_models == (model,)) == (max(counts) > 0)
    expected = accelerator_of_gpu_model(model)
    assert (hardware.accelerator is expected) == (max(counts) > 0)


@pytest.mark.parametrize(
    ("model", "accelerator"),
    [
        ("nvidia_h100_80gb_hbm3", Accelerator.CUDA),
        ("a100", Accelerator.CUDA),
        ("l40s", Accelerator.CUDA),
        ("mi300x", Accelerator.ROCM),
        ("mi250", Accelerator.ROCM),
        ("apple_m2", Accelerator.METAL),
        ("quantum", None),
    ],
)
def test_a_gpu_model_runs_on_the_accelerator_its_vendor_names(
    model: str, accelerator: Accelerator | None
) -> None:
    assert accelerator_of_gpu_model(model) is accelerator


def test_a_gpu_that_sinfo_does_not_type_has_no_known_accelerator() -> None:
    _, hardware = parse_sinfo("p|1|gpu:4\n")

    assert hardware == ClusterHardware(accelerator=None, gpus_per_node=4)


# --- the settings file --------------------------------------------------------------------


@given(cluster=_clusters())
def test_an_added_cluster_is_listed_with_exactly_the_settings_it_was_added_with(
    cluster: SlurmClusterSettings,
) -> None:
    compute = fake_compute(script=_passing)

    settings = compute.add(cluster)

    assert settings.config_path == _CONFIG
    ((listed,),) = [settings.clusters]
    assert listed.cluster == cluster
    assert listed.source is ClusterSource.VIBESYS
    # A second reading of the same file says the same.
    assert compute.settings() == settings


@given(cluster=_clusters())
def test_removing_the_cluster_leaves_the_machine_as_it_was(cluster: SlurmClusterSettings) -> None:
    compute = fake_compute(script=_passing)
    before = compute.settings()

    compute.add(cluster)
    after = compute.remove(cluster.name)

    assert after == before


@given(cluster=_clusters())
def test_a_cluster_that_fails_its_test_is_not_saved(cluster: SlurmClusterSettings) -> None:
    files = FakeConfigFiles()
    compute = fake_compute(files, _failing(ClusterTestStage.WORKSPACE))
    if isinstance(cluster.reach, SshReach):
        compute = fake_compute(files, _failing(ClusterTestStage.REACH))

    with pytest.raises(ComputeError) as raised:
        compute.add(cluster)

    assert raised.value.code is HelperErrorCode.CLUSTER_TEST_FAILED
    assert files.writes == []


def test_a_name_already_set_up_is_refused_and_leaves_the_file_alone() -> None:
    files = FakeConfigFiles()
    compute = fake_compute(files, _passing)
    compute.add(_ssh_cluster())
    saved = files.files[_CONFIG]

    with pytest.raises(ComputeError) as raised:
        compute.add(_ssh_cluster(workspace="/elsewhere"))

    assert raised.value.code is HelperErrorCode.CLUSTER_EXISTS
    assert files.files[_CONFIG] == saved


def test_removing_a_cluster_that_is_not_set_up_says_so() -> None:
    with pytest.raises(ComputeError) as raised:
        fake_compute().remove("ghost")

    assert raised.value.code is HelperErrorCode.UNKNOWN_CLUSTER


@pytest.mark.parametrize(
    ("fields", "named"),
    [
        ({"time_limit": "0:10"}, "time_limit"),
        ({"workspace": "scratch/relative"}, "workspace"),
        ({"sbatch_options": ("--requeue",)}, "sbatch_options"),
    ],
)
def test_settings_the_slurm_configuration_rejects_name_the_key_to_edit(
    fields: dict[str, object], named: str
) -> None:
    compute = fake_compute(script=_passing)

    for call in (compute.test, compute.add):
        with pytest.raises(ComputeError) as raised:
            call(_ssh_cluster(**fields))
        assert raised.value.code is HelperErrorCode.CLUSTER_INVALID
        assert named in str(raised.value)


@pytest.mark.parametrize(
    "text",
    [
        "not toml [",
        "version = 2\n",
        "version = 1\nextra = 3\n",
        'version = 1\n[slurm.lab]\nreach = {kind = "login_node"}\nworkspace = "/w"\nbogus = 1\n',
    ],
)
def test_a_settings_file_with_an_unknown_key_or_version_is_refused_naming_its_path(
    text: str,
) -> None:
    compute = fake_compute(FakeConfigFiles({_CONFIG: text}))

    with pytest.raises(ComputeError) as raised:
        compute.settings()

    assert raised.value.code is HelperErrorCode.COMPUTE_CONFIG_UNREADABLE
    assert raised.value.path == _CONFIG


def test_a_settings_file_that_cannot_be_read_is_refused_naming_its_path() -> None:
    compute = fake_compute(FakeConfigFiles(unreadable={_CONFIG}))

    with pytest.raises(ComputeError) as raised:
        compute.settings()

    assert raised.value.code is HelperErrorCode.COMPUTE_CONFIG_UNREADABLE
    assert raised.value.path == _CONFIG


def test_the_operator_slurm_file_of_a_terminal_run_is_listed_and_only_read() -> None:
    operator = (
        '[slurm]\nname = "hpc"\nremote_workspace_root = "/work/v"\n'
        'sbatch_arguments = ["--partition=gpu"]\n[slurm.transport]\nkind = "ssh"\nhost = "hpc-login"\n'
    )
    files = FakeConfigFiles({_OPERATOR: operator})
    compute = fake_compute(files)

    (listed,) = compute.settings().clusters

    assert listed.source is ClusterSource.SLURM_TOML
    assert listed.cluster.name == "hpc"
    assert listed.cluster.reach == SshReach(host="hpc-login")
    assert listed.cluster.sbatch_options == ("--partition=gpu",)
    assert compute.slurm_config_path("hpc") == _OPERATOR
    with pytest.raises(ComputeError) as removed:
        compute.remove("hpc")
    assert removed.value.code is HelperErrorCode.CLUSTER_READ_ONLY
    with pytest.raises(ComputeError) as added:
        compute.add(_ssh_cluster(name="hpc"))
    assert added.value.code is HelperErrorCode.CLUSTER_EXISTS
    assert files.writes == []


@given(cluster=_clusters())
def test_a_cluster_runs_through_an_operator_file_that_says_what_the_settings_say(
    cluster: SlurmClusterSettings,
) -> None:
    files = FakeConfigFiles()
    compute = fake_compute(files, _passing)
    compute.add(cluster)

    path = compute.slurm_config_path(cluster.name)

    assert path is not None
    assert path != _CONFIG
    table = tomllib.loads(files.files[path])["slurm"]
    assert table["name"] == cluster.name
    assert table["remote_workspace_root"] == cluster.workspace
    transport = table["transport"]
    assert transport["kind"] == ("ssh" if isinstance(cluster.reach, SshReach) else "local")
    assert transport.get("host") == (
        cluster.reach.host if isinstance(cluster.reach, SshReach) else None
    )
    # The run reads the file back as the cluster's own configuration, whole.
    assert SlurmConfig.model_validate(table, strict=True) == slurm_config_of(cluster)
    expected = [
        *([f"--partition={cluster.partition}"] if cluster.partition else []),
        *([f"--nodes={cluster.nodes}"] if cluster.nodes else []),
        *([f"--time={cluster.time_limit}"] if cluster.time_limit else []),
        *cluster.sbatch_options,
    ]
    assert table["sbatch_arguments"] == expected
    assert compute.slurm_config_path("ghost") is None


# --- testing a cluster --------------------------------------------------------------------


def test_a_cluster_that_answers_reports_the_hardware_sinfo_lists() -> None:
    result = fake_compute(script=_passing).test(_ssh_cluster())

    assert isinstance(result, ClusterTestPassed)
    assert result.partitions == ("gpu", "cpu")
    assert result.hardware.accelerator is Accelerator.CUDA


@pytest.mark.parametrize("stage", list(ClusterTestStage))
def test_each_way_a_connection_fails_is_reported_at_its_own_stage(stage: ClusterTestStage) -> None:
    result = fake_compute(script=_failing(stage)).test(_ssh_cluster())

    assert isinstance(result, ClusterTestFailed)
    assert result.stage is stage


def test_a_login_node_runs_its_commands_here_and_an_ssh_cluster_over_ssh() -> None:
    calls: list[tuple[str, ...]] = []

    def script(argv: tuple[str, ...]) -> ProbeResult | None:
        calls.append(argv)
        return _passing(argv)

    fake_compute(script=script).test(_ssh_cluster(reach=LoginNodeReach()))
    login_calls, calls[:] = list(calls), []
    fake_compute(script=script).test(_ssh_cluster(partition="gpu"))

    assert all(call[0] != "ssh" for call in login_calls)
    assert calls[0][-1] == "true"
    assert all(call[0] == "ssh" and "BatchMode=yes" in call for call in calls)
    assert shlex.split(calls[1][-1])[-2:] == ["-p", "gpu"]


def test_a_machine_that_cannot_run_a_command_fails_the_stage_that_needed_it() -> None:
    unreachable = fake_compute().test(_ssh_cluster())
    assert isinstance(unreachable, ClusterTestFailed)
    assert unreachable.stage is ClusterTestStage.REACH
    login = fake_compute().test(_ssh_cluster(reach=LoginNodeReach()))
    assert isinstance(login, ClusterTestFailed)
    assert login.stage is ClusterTestStage.SCHEDULER


# --- Modal --------------------------------------------------------------------------------


@given(
    token_id=st.text(alphabet="abcdef0123456789", min_size=1, max_size=20),
    secret=st.text(alphabet="ghijklmnopqrstuvwxyz", min_size=8, max_size=40),
)
def test_a_modal_token_is_reported_present_and_the_secret_is_never_in_what_is_sent(
    token_id: str, secret: str
) -> None:
    environment = fake_compute(
        environ={"MODAL_TOKEN_ID": token_id, "MODAL_TOKEN_SECRET": secret}
    ).settings()
    profile_file = f'[work]\ntoken_id = "{token_id}"\ntoken_secret = "{secret}"\n'
    profile = fake_compute(FakeConfigFiles({f"{HOME}/.modal.toml": profile_file})).settings()

    assert environment.modal == ModalStatus(token_present=True, source=ModalTokenSource.ENVIRONMENT)
    assert profile.modal == ModalStatus(token_present=True, source=ModalTokenSource.PROFILE)
    for settings in (environment, profile):
        assert secret not in settings.model_dump_json()


@pytest.mark.parametrize(
    ("environ", "profile"),
    [
        ({}, None),
        ({"MODAL_TOKEN_ID": "ak-1"}, None),
        ({"MODAL_TOKEN_ID": " ", "MODAL_TOKEN_SECRET": "s"}, None),
        ({}, '[work]\ntoken_id = "ak-1"\n'),
        ({}, "not toml ["),
    ],
)
def test_modal_is_absent_unless_a_token_id_and_its_secret_are_both_held(
    environ: dict[str, str], profile: str | None
) -> None:
    files = FakeConfigFiles({} if profile is None else {f"{HOME}/.modal.toml": profile})

    assert fake_compute(files, environ=environ).modal() == ModalStatus(token_present=False)


def test_modal_status_names_its_source_exactly_when_a_token_is_present() -> None:
    with pytest.raises(ValidationError):
        ModalStatus(token_present=True)
    with pytest.raises(ValidationError):
        ModalStatus(token_present=False, source=ModalTokenSource.PROFILE)


@given(gpus=st.integers(1, 8), models=st.lists(st.just("h100"), max_size=1))
def test_a_cpu_cluster_cannot_claim_gpus(gpus: int, models: list[str]) -> None:
    with pytest.raises(ValidationError):
        ClusterHardware(accelerator=Accelerator.CPU, gpu_models=tuple(models), gpus_per_node=gpus)


def test_the_toml_writer_round_trips_what_the_settings_file_holds() -> None:
    document = {
        "version": 1,
        "slurm": {"a.b": {"workspace": '/w"x', "nodes": 2, "ok": True, "o": ["x"]}},
    }

    assert tomllib.loads(render_toml(document)) == document


# --- where a run is measured --------------------------------------------------------------


def _request[R: RunStartRequest | RunPreflightRequest](kind: type[R], **fields: object) -> R:
    return kind.model_validate({"request_id": "r", "deadline_ms": 5000, "project": "/p", **fields})


def _project(root: Path) -> Path:
    task = root / ".vibesys" / "tasks" / "spsc"
    task.mkdir(parents=True)
    (task / "OBJECTIVE.md").write_text("Make it fast.\n", encoding="utf-8")
    (task / "vibesys.input.toml").write_text("", encoding="utf-8")
    return root


class _Preflight:
    def __init__(self, backend: Backend | None = Backend.CUDA) -> None:
        self.calls: list[tuple[Path, tuple[str, ...]]] = []
        self._backend = backend

    def report(self, project: Path, arguments: tuple[str, ...]) -> PreflightReport:
        self.calls.append((project, arguments))
        return PreflightReport(
            settings=SettingsValid(),
            uses_docker=False,
            agent_image=Check(id=CheckId.AGENT_IMAGE, state=CheckState.OK, detail="cached"),
            evaluator=Check(id=CheckId.EVALUATOR_AVAILABLE, state=CheckState.OK, detail="bench"),
            backend=self._backend,
        )


def _run(
    request: RunStartRequest | RunPreflightRequest,
    project: Path,
    compute: MachineCompute,
    backend: Backend | None = Backend.CUDA,
) -> tuple[object, FakeRunLauncher, _Preflight]:
    started = record("0123456789ab", project_root=str(project))
    launcher = FakeRunLauncher(lambda _project, _arguments: started)
    preflight = _Preflight(backend)
    threads = SimThreads()
    host = replace(
        fake_host(threads, SimNetwork(threads), launcher=launcher),
        preflight=preflight,
        compute=compute,
    )
    shaped = request.model_copy(update={"project": str(project)})
    return answer(shaped, host, threads.event()), launcher, preflight


def _with_cluster(tmp_path: Path) -> tuple[Path, MachineCompute, str]:
    compute = fake_compute(FakeConfigFiles(), _passing, environ={})
    compute.add(
        _ssh_cluster(hardware=ClusterHardware(accelerator=Accelerator.CUDA, gpus_per_node=8))
    )
    path = compute.slurm_config_path("lab")
    assert path is not None
    return _project(tmp_path / "proj"), compute, path


def test_a_run_measured_on_a_cluster_selects_the_slurm_environment_over_its_operator_file(
    tmp_path: Path,
) -> None:
    project, compute, path = _with_cluster(tmp_path)
    request = _request(RunStartRequest, measure_on={"kind": "slurm", "cluster": "lab"})

    reply, launcher, _ = _run(request, project, compute)

    assert isinstance(reply, RunStartReply)
    ((_, launched),) = launcher.calls
    environment = (RUN_ENVIRONMENT_FLAG, "slurm", SLURM_CONFIG_FLAG, path)
    assert launched == launch_arguments(request, environment)
    index = launched.index(RUN_ENVIRONMENT_FLAG)
    assert launched[index : index + 4] == environment


def test_a_run_measured_on_modal_selects_the_modal_environment_with_a_token(
    tmp_path: Path,
) -> None:
    compute = fake_compute(environ={"MODAL_TOKEN_ID": "a", "MODAL_TOKEN_SECRET": "b"})
    request = _request(RunStartRequest, measure_on={"kind": "modal"})

    reply, launcher, _ = _run(request, _project(tmp_path / "p"), compute)

    assert isinstance(reply, RunStartReply)
    ((_, launched),) = launcher.calls
    assert launched[launched.index(RUN_ENVIRONMENT_FLAG) :][:2] == (RUN_ENVIRONMENT_FLAG, "modal")
    assert SLURM_CONFIG_FLAG not in launched


@pytest.mark.parametrize("measure_on", [None, {"kind": "same_machine"}])
def test_a_run_measured_on_the_same_machine_adds_no_environment_flag(
    tmp_path: Path, measure_on: dict[str, str] | None
) -> None:
    request = _request(RunStartRequest, measure_on=measure_on)

    reply, launcher, _ = _run(request, _project(tmp_path / "p"), fake_compute())

    assert isinstance(reply, RunStartReply)
    ((_, launched),) = launcher.calls
    assert launched == launch_arguments(request)
    assert RUN_ENVIRONMENT_FLAG not in launched


@pytest.mark.parametrize(
    ("measure_on", "code"),
    [
        ({"kind": "slurm", "cluster": "ghost"}, HelperErrorCode.UNKNOWN_CLUSTER),
        ({"kind": "modal"}, HelperErrorCode.MODAL_NOT_CONFIGURED),
    ],
)
def test_a_target_that_is_not_set_up_here_starts_nothing(
    tmp_path: Path, measure_on: dict[str, str], code: HelperErrorCode
) -> None:
    request = _request(RunStartRequest, measure_on=measure_on)

    reply, launcher, _ = _run(request, _project(tmp_path / "p"), fake_compute())

    assert isinstance(reply, HelperError)
    assert reply.code is code
    assert launcher.calls == []


_ENVIRONMENT_TOKENS = [
    "--run-environment=docker",
    "--run-environment",
    "--run-env=slurm",
    "--slurm-config=/x.toml",
    "--slurm",
    "--modal",
    "--mod",
    "--skypilot",
    "--sky",
]


@pytest.mark.parametrize("token", _ENVIRONMENT_TOKENS)
@pytest.mark.parametrize("kind", [RunStartRequest, RunPreflightRequest])
def test_free_text_arguments_may_not_choose_the_environment_next_to_a_target(
    tmp_path: Path, token: str, kind: type[RunStartRequest | RunPreflightRequest]
) -> None:
    request = _request(kind, measure_on={"kind": "same_machine"}, args=[token])

    reply, launcher, preflight = _run(request, _project(tmp_path / "p"), fake_compute())

    assert isinstance(reply, HelperError)
    assert reply.code is HelperErrorCode.INVALID_REQUEST
    assert token.split("=", maxsplit=1)[0] in reply.message or "measure_on" in reply.message
    assert launcher.calls == []
    assert preflight.calls == []


def test_without_a_target_the_environment_flags_stay_the_terminals_to_set(tmp_path: Path) -> None:
    request = _request(RunStartRequest, args=["--modal-gpu", "A100", "--modal"])

    reply, launcher, _ = _run(request, _project(tmp_path / "p"), fake_compute())

    assert isinstance(reply, RunStartReply)
    assert launcher.calls[0][1][-3:] == ("--modal-gpu", "A100", "--modal")


def test_a_target_with_other_run_options_keeps_their_order(tmp_path: Path) -> None:
    project, compute, path = _with_cluster(tmp_path)
    request = _request(
        RunStartRequest,
        task="spsc",
        measure_on={"kind": "slurm", "cluster": "lab"},
        args=["--max-rounds", "3"],
    )

    reply, launcher, _ = _run(request, project, compute)

    assert isinstance(reply, RunStartReply)
    ((_, launched),) = launcher.calls
    assert launched[:3] == ("--detach", "--task", "spsc")
    assert launched[-2:] == ("--max-rounds", "3")
    assert (
        run_arguments(request, (RUN_ENVIRONMENT_FLAG, "slurm", SLURM_CONFIG_FLAG, path))
        == launched[1:]
    )


# --- preflight ----------------------------------------------------------------------------


def _measurement(reply: object) -> Check:
    assert isinstance(reply, RunPreflightReply)
    assert reply.checks[-1].id is CheckId.MEASUREMENT_TARGET
    assert [check.id for check in reply.checks].count(CheckId.MEASUREMENT_TARGET) == 1
    return reply.checks[-1]


def test_preflight_without_a_target_has_no_measurement_check(tmp_path: Path) -> None:
    reply, _, _ = _run(_request(RunPreflightRequest), _project(tmp_path / "p"), fake_compute())

    assert isinstance(reply, RunPreflightReply)
    assert CheckId.MEASUREMENT_TARGET not in [check.id for check in reply.checks]


def test_preflight_reports_a_cluster_that_is_not_set_up_with_the_fix(tmp_path: Path) -> None:
    request = _request(RunPreflightRequest, measure_on={"kind": "slurm", "cluster": "ghost"})

    reply, _, preflight = _run(request, _project(tmp_path / "p"), fake_compute())

    check = _measurement(reply)
    target = SlurmTarget(cluster="ghost")
    assert check.state is CheckState.FAIL
    assert check.cause == MeasurementTargetNotConfigured(target=target)
    assert check.fix == ConfigureCompute(target=target)
    assert RUN_ENVIRONMENT_FLAG not in preflight.calls[0][1]


def test_preflight_reports_modal_without_a_token(tmp_path: Path) -> None:
    request = _request(RunPreflightRequest, measure_on={"kind": "modal"})

    reply, _, _ = _run(request, _project(tmp_path / "p"), fake_compute())

    check = _measurement(reply)
    assert check.state is CheckState.FAIL
    assert check.cause == MeasurementTargetNotConfigured(target=ModalTarget())


def test_preflight_runs_the_runs_own_checks_with_the_target_the_start_would_use(
    tmp_path: Path,
) -> None:
    project, compute, path = _with_cluster(tmp_path)
    request = _request(RunPreflightRequest, measure_on={"kind": "slurm", "cluster": "lab"})

    reply, _, preflight = _run(request, project, compute)

    assert _measurement(reply).state is CheckState.OK
    (_, arguments), *_ = preflight.calls
    assert arguments == run_arguments(
        request, (RUN_ENVIRONMENT_FLAG, "slurm", SLURM_CONFIG_FLAG, path)
    )


@given(
    backend=st.sampled_from(list(Backend)),
    offered=st.sampled_from(list(Accelerator)),
)
def test_a_task_fits_a_place_exactly_when_the_place_runs_its_backend(
    backend: Backend, offered: Accelerator
) -> None:
    fits = measurement_fits(backend, offered)

    assert fits == (backend is Backend.CPU or backend.value == offered.value)
    assert measurement_fits(Backend.CPU, offered)
    assert not measurement_fits(Backend.TRAINIUM, offered)


@given(backend=st.sampled_from(list(Backend)), cluster=_clusters())
def test_a_cluster_whose_hardware_cannot_run_the_backend_fails_the_check_with_both_facts(
    backend: Backend, cluster: SlurmClusterSettings
) -> None:
    compute = fake_compute(script=_passing)
    settings = compute.add(cluster)
    target = SlurmTarget(cluster=cluster.name)

    check = measurement_check(target, settings, backend)

    hardware = cluster.hardware
    if hardware is None or hardware.accelerator is None:
        assert check.state is CheckState.WARN
    elif measurement_fits(backend, hardware.accelerator):
        assert check.state is CheckState.OK
    else:
        assert check.state is CheckState.FAIL
        assert check.cause == MeasurementTargetMisfit(
            target=target, backend=backend, offered=hardware.accelerator
        )


def test_the_task_backend_unknown_passes_with_a_warning_and_modal_offers_cuda() -> None:
    settings = fake_compute(environ={"MODAL_TOKEN_ID": "a", "MODAL_TOKEN_SECRET": "b"}).settings()

    unknown = measurement_check(ModalTarget(), settings, None)
    cuda = measurement_check(ModalTarget(), settings, Backend.CUDA)
    metal = measurement_check(ModalTarget(), settings, Backend.METAL)

    assert unknown.state is CheckState.WARN
    assert cuda.state is CheckState.OK
    assert metal.state is CheckState.FAIL
    assert measurement_check(SameMachine(), settings, Backend.METAL).state is CheckState.OK


# --- the messages -------------------------------------------------------------------------


def test_compute_requests_need_machine_compute_and_measure_on_needs_measurement_target() -> None:
    base = {"request_id": "r", "deadline_ms": 5000}
    cluster = {"name": "lab", "reach": {"kind": "login_node"}, "workspace": "/w"}
    lines = [
        {**base, "type": "compute.list"},
        {**base, "type": "compute.test_cluster", "cluster": cluster},
        {**base, "type": "compute.add_cluster", "cluster": cluster},
        {**base, "type": "compute.remove_cluster", "name": "lab"},
    ]
    for line in lines:
        request = parse_helper_request(json.dumps(line).encode())
        assert not isinstance(request, Unreadable)
        assert request_features(request) == (HelperFeature.MACHINE_COMPUTE,)
    start = _request(RunStartRequest, measure_on={"kind": "modal"})
    preflight = _request(RunPreflightRequest, measure_on={"kind": "same_machine"})
    assert request_features(start) == (HelperFeature.MEASUREMENT_TARGET,)
    assert request_features(preflight) == (
        HelperFeature.PREFLIGHT,
        HelperFeature.MEASUREMENT_TARGET,
    )
    assert request_features(_request(RunStartRequest)) == ()


@given(extra=st.text(alphabet="abcdefgh", min_size=1, max_size=8))
def test_an_unknown_key_in_a_cluster_or_target_is_rejected(extra: str) -> None:
    cluster = {"name": "lab", "reach": {"kind": "login_node"}, "workspace": "/w", extra: 1}
    with pytest.raises(ValidationError):
        SlurmClusterSettings.model_validate(cluster)
    with pytest.raises(ValidationError):
        _request(RunStartRequest, measure_on={"kind": "modal", extra: 1})
    with pytest.raises(ValidationError):
        _request(RunStartRequest, measure_on={"kind": extra})


def test_the_compute_requests_are_answered_over_the_machines_own_settings() -> None:
    files = FakeConfigFiles()
    compute = fake_compute(files, _passing)
    threads = SimThreads()
    host = replace(fake_host(threads, SimNetwork(threads)), compute=compute)
    cluster = _ssh_cluster()

    def ask(request: HelperRequest) -> HelperReply | HelperError:
        return answer(request, host, threads.event())

    tested = ask(ComputeTestClusterRequest(request_id="t", deadline_ms=5000, cluster=cluster))
    added = ask(ComputeAddClusterRequest(request_id="a", deadline_ms=5000, cluster=cluster))
    listed = ask(ComputeListRequest(request_id="l", deadline_ms=5000))
    again = ask(ComputeAddClusterRequest(request_id="b", deadline_ms=5000, cluster=cluster))
    removed = ask(ComputeRemoveClusterRequest(request_id="d", deadline_ms=5000, name="lab"))
    gone = ask(ComputeRemoveClusterRequest(request_id="e", deadline_ms=5000, name="lab"))

    assert isinstance(tested, ComputeTestClusterReply)
    assert isinstance(tested.result, ClusterTestPassed)
    assert isinstance(added, ComputeAddClusterReply)
    assert isinstance(listed, ComputeListReply)
    assert listed.settings == added.settings
    assert isinstance(again, HelperError)
    assert again.code is HelperErrorCode.CLUSTER_EXISTS
    assert isinstance(removed, ComputeRemoveClusterReply)
    assert removed.settings.clusters == ()
    assert isinstance(gone, HelperError)
    assert gone.code is HelperErrorCode.UNKNOWN_CLUSTER


def test_a_settings_file_that_is_wrong_answers_the_request_with_its_path() -> None:
    compute = fake_compute(FakeConfigFiles({_CONFIG: "version = 9\n"}))
    threads = SimThreads()
    host = replace(fake_host(threads, SimNetwork(threads)), compute=compute)

    reply = answer(ComputeListRequest(request_id="r", deadline_ms=5000), host, threads.event())

    assert isinstance(reply, HelperError)
    assert reply.code is HelperErrorCode.COMPUTE_CONFIG_UNREADABLE
    assert reply.path == _CONFIG


@given(argv=st.lists(st.text(max_size=20).filter(lambda text: "\x00" not in text), max_size=5))
def test_a_remote_command_line_gives_the_shell_back_exactly_the_arguments(
    argv: list[str],
) -> None:
    assert shlex.split(remote_command(argv)) == argv
