"""The host's compute settings: a machine's Slurm clusters, its Modal token, connection tests.

``MachineCompute`` is the one place that reads and writes what a machine offers as places to
measure. Its Slurm clusters live in one TOML file the host edits (``compute.toml`` in the
user's VibeSys configuration directory). The operator file a terminal run reads
(``slurm.toml``, one cluster) is listed beside them and only read, so a machine that already
runs Slurm through ``--slurm-config`` shows that cluster without a second copy. A cluster of
the host's file is rendered, when a run needs it, into the same ``[slurm]`` operator format
(one file per cluster beside ``compute.toml``), so a run measures through the existing
``--run-environment slurm --slurm-config PATH`` wiring and no second path. The rendered file
is derived from ``compute.toml`` on every use and never edited by hand.

Files go through ``ConfigFiles`` and commands (``ssh``, ``sinfo``) through ``vs_sim``'s
``CommandProbe``, so tests drive it with in-memory files and a ``ScriptedProbe``. A Modal
token is only ever tested for, never read into a reply. Light like the helper
(``tests/architecture/test_light_entrypoints.py``): the standard library, Pydantic,
``vs_slurm`` (through ``server.host_slurm``, loaded on first use) and light ``server``
modules.

Public surface: ``MachineCompute``, ``ConfigFiles``, ``OsConfigFiles``, ``COMPUTE_FILE``,
``SLURM_FILE``, ``parse_sinfo``, ``accelerator_of_gpu_model``, ``render_toml``, ``remote_command``.
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from server.host_compute import (
    ClusterHardware,
    ClusterName,
    ClusterSource,
    ClusterTestFailed,
    ClusterTestPassed,
    ClusterTestResult,
    ClusterTestStage,
    ComputeError,
    ComputeSettings,
    ListedCluster,
    ModalStatus,
    ModalTokenSource,
    SlurmClusterSettings,
    SshReach,
)
from server.host_envelope import HelperErrorCode
from server.host_machine_facts import Accelerator

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from types import ModuleType

    from vs_sim.api import CommandProbe, ProbeResult

CONFIG_DIRECTORY: Final = ".config/vibesys"
"""Where VibeSys keeps a user's machine-wide configuration, under the home directory."""
COMPUTE_FILE: Final = "compute.toml"
"""The file this host edits: the Slurm clusters set up on the machine."""
SLURM_FILE: Final = "slurm.toml"
"""The operator file a terminal run reads by default; listed, never written."""
_RENDERED_DIRECTORY: Final = "compute"
_FORMAT_VERSION: Final = 1
"""The ``version`` of ``compute.toml`` this helper writes and reads."""
_REACH_TIMEOUT_S: Final = 15.0
_SINFO_TIMEOUT_S: Final = 30.0
_WORKSPACE_TIMEOUT_S: Final = 15.0
_SSH_OPTIONS: Final = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")
_SINFO_COLUMNS: Final = ("partition", "nodes", "gres")
_SINFO: Final = ("sinfo", "-h", "-o", "%P|%D|%G")
_MODAL_PROFILE_FILE: Final = ".modal.toml"
_MODAL_VARIABLES: Final = ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET")
"""The variables that together hold a Modal token; only whether both are set is ever read."""
_MODAL_CONFIG_VARIABLE: Final = "MODAL_CONFIG_PATH"
_LINE_MAX: Final = 300


class ConfigFiles(Protocol):
    """Reads and writes small text files on the machine.

    Contract: ``read`` returns ``None`` for a file that does not exist and raises ``OSError``
    for one that cannot be read; ``write`` creates missing directories and replaces the file
    whole, so a reader never sees half a file.
    """

    def read(self, path: str) -> str | None:
        """The file's text, or ``None`` when it does not exist."""
        ...

    def write(self, path: str, text: str) -> None:
        """Replace the file with ``text``."""
        ...


def accelerator_of_gpu_model(model: str) -> Accelerator | None:
    """How a GPU model as ``sinfo`` names it runs work, or ``None`` for a name no vendor claims."""
    name = model.lower()
    for pattern, accelerator in _GPU_FAMILIES:
        if pattern.match(name):
            return accelerator
    return None


_GPU_FAMILIES: Final = (
    (re.compile(r"^(mi\d+|amd|radeon|instinct)"), Accelerator.ROCM),
    (
        re.compile(r"^(nvidia|tesla|geforce|quadro|rtx|gh\d+|gb\d+|[abhlpvtk]\d{1,3}(?![a-z]))"),
        Accelerator.CUDA,
    ),
    (re.compile(r"^apple"), Accelerator.METAL),
)
"""Each family of GPU model names with the accelerator it runs on, in the order tried."""

_PARENTHESIZED = re.compile(r"\([^)]*\)")


def _gpus_of(gres: str) -> tuple[str | None, int] | None:
    """The model (``None`` when untyped) and count of one ``gpu`` entry of a ``%G`` field.

    An entry is ``gpu:COUNT`` or ``gpu:MODEL[:no_consume]:COUNT``, each optionally followed by a
    parenthesized note such as the sockets it is bound to.
    """
    parts = _PARENTHESIZED.sub("", gres).strip().split(":")
    if parts[0] != "gpu" or not parts[-1].isdigit():
        return None
    named = [part for part in parts[1:-1] if part and part != "no_consume"]
    return (named[0] if named else None), int(parts[-1])


def parse_sinfo(output: str) -> tuple[tuple[str, ...], ClusterHardware]:
    """The partitions and the hardware ``sinfo -h -o '%P|%D|%G'`` lists.

    Total: a line that is not ``PARTITION|NODES|GRES`` is skipped. The hardware is the
    largest GPU count a node lists and every model named; its accelerator is the one every
    model runs on, and ``None`` when some GPU's vendor is unknown.
    """
    partitions: list[str] = []
    models: set[str] = set()
    per_node = 0
    untyped = False
    for line in output.splitlines():
        columns = line.strip().split("|")
        if len(columns) != len(_SINFO_COLUMNS):
            continue
        name = columns[0].strip().rstrip("*")
        if not name:
            continue
        if name not in partitions:
            partitions.append(name)
        for entry in columns[_SINFO_COLUMNS.index("gres")].split(","):
            gpus = _gpus_of(entry)
            if gpus is None or gpus[1] == 0:
                continue
            per_node = max(per_node, gpus[1])
            if gpus[0] is None:
                untyped = True
            else:
                models.add(gpus[0])
    if per_node == 0:
        return tuple(partitions), ClusterHardware(accelerator=Accelerator.CPU)
    kinds = {accelerator_of_gpu_model(model) for model in models}
    accelerator = None if untyped or len(kinds) != 1 else next(iter(kinds))
    return tuple(partitions), ClusterHardware(
        accelerator=accelerator, gpu_models=tuple(sorted(models)), gpus_per_node=per_node
    )


def render_toml(document: Mapping[str, object]) -> str:
    """``document`` as TOML: tables nest, and values are text, numbers, booleans or lists."""
    lines: list[str] = []
    _emit((), document, lines)
    return "\n".join(lines) + "\n"


def _emit(path: tuple[str, ...], table: Mapping[str, object], lines: list[str]) -> None:
    if path:
        if lines:
            lines.append("")
        lines.append(f"[{'.'.join(_key(part) for part in path)}]")
    nested: list[tuple[str, Mapping[str, object]]] = []
    for key, value in table.items():
        if isinstance(value, dict):
            nested.append((key, value))
        else:
            lines.append(f"{_key(key)} = {_value(value)}")
    for key, value in nested:
        _emit((*path, key), value, lines)


def _key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_-]+", key) else json.dumps(key)


def _value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")
    if isinstance(value, list | tuple):
        return "[" + ", ".join(_value(item) for item in value) + "]"
    message = f"cannot render {type(value).__name__} as TOML"
    raise TypeError(message)


def _stored(cluster: SlurmClusterSettings) -> dict[str, object]:
    """``cluster`` as the table of ``compute.toml`` that names it."""
    return cluster.model_dump(mode="json", exclude={"name"}, exclude_none=True)


_SETTING_OF: Final = {
    "remote_workspace_root": "workspace",
    "transport": "reach",
    "sbatch_arguments": "partition, nodes, time_limit or sbatch_options",
    "process_exit_wait_seconds": "time_limit",
}
"""The setting a ``vs_slurm`` configuration field is made from, to name the key a person edits."""


def _invalid_keys(error: ValidationError | ValueError) -> str:
    """The settings an invalid configuration names, as a person edits them."""
    if isinstance(error, ValidationError):
        keys = {str(item["loc"][0]) if item["loc"] else "cluster" for item in error.errors()}
        return ", ".join(sorted(_SETTING_OF.get(key, key) for key in keys))
    return str(error)


def _slurm() -> ModuleType:
    """The ``vs_slurm`` translation, imported on first use."""
    # lint-waiver: LW-271611 [PLC0415]; the helper answers hello without loading vs_slurm.
    # > `vs_slurm` is about thirty modules the helper's start-up budget
    # > (`tests/e2e/test_host_helper.py`) has no room for, and only a compute request needs it;
    # > a copy of its validation here would be a second source of truth for what a cluster is.
    from server import host_slurm  # noqa: PLC0415

    return host_slurm


class _ComputeFile(BaseModel):
    """The shape of ``compute.toml``: a version and one table per cluster; nothing else."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1]
    slurm: dict[ClusterName, dict[str, JsonValue]] = Field(default_factory=dict)


class MachineCompute:
    """What this machine offers as places to measure, read and changed through its files.

    ``home`` is the user's home directory and ``environ`` the environment a run starts in
    (it tells whether Modal's token variables are set). Every call reads the files afresh,
    so a second helper or a terminal edit is never shadowed by a cache.
    """

    def __init__(
        self,
        files: ConfigFiles,
        probe: CommandProbe,
        *,
        home: str,
        environ: Mapping[str, str],
    ) -> None:
        """Bind the machine's files, its command probe, and the home and environment."""
        self._files = files
        self._probe = probe
        self._home = home
        self._environ = environ

    @property
    def config_path(self) -> str:
        """The file of this machine that holds the clusters this helper edits."""
        return posixpath.join(self._home, CONFIG_DIRECTORY, COMPUTE_FILE)

    @property
    def _operator_path(self) -> str:
        return posixpath.join(self._home, CONFIG_DIRECTORY, SLURM_FILE)

    def _rendered_path(self, name: str) -> str:
        return posixpath.join(
            self._home, CONFIG_DIRECTORY, _RENDERED_DIRECTORY, f"slurm-{name}.toml"
        )

    def settings(self) -> ComputeSettings:
        """The clusters set up here, the host's file first, and Modal's token status."""
        listed = [
            ListedCluster(cluster=cluster, source=ClusterSource.VIBESYS)
            for cluster in self._stored_clusters().values()
        ]
        names = {each.cluster.name for each in listed}
        operator = self._operator_cluster()
        if operator is not None and operator.name not in names:
            listed.append(ListedCluster(cluster=operator, source=ClusterSource.SLURM_TOML))
        return ComputeSettings(
            config_path=self.config_path, clusters=tuple(listed), modal=self.modal()
        )

    def modal(self) -> ModalStatus:
        """Whether a Modal token is present here, and where it is held; never the token."""
        if all(self._environ.get(name, "").strip() for name in _MODAL_VARIABLES):
            return ModalStatus(token_present=True, source=ModalTokenSource.ENVIRONMENT)
        path = self._environ.get(_MODAL_CONFIG_VARIABLE) or posixpath.join(
            self._home, _MODAL_PROFILE_FILE
        )
        try:
            text = self._files.read(path)
            document = tomllib.loads(text) if text is not None else {}
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
            document = {}
        for profile in document.values():
            if (
                isinstance(profile, dict)
                and isinstance(profile.get("token_id"), str)
                and profile["token_id"].strip()
                and isinstance(profile.get("token_secret"), str)
                and profile["token_secret"].strip()
            ):
                return ModalStatus(token_present=True, source=ModalTokenSource.PROFILE)
        return ModalStatus(token_present=False)

    def slurm_config_path(self, name: str) -> str | None:
        """The operator file a run measures on cluster ``name`` through, or ``None`` if none is set up.

        A cluster of the host's file is rendered to its own operator file first, so the file
        always says what ``compute.toml`` does.
        """
        stored = self._stored_clusters().get(name)
        if stored is not None:
            try:
                config = _slurm().slurm_config_of(stored)
            except (ValidationError, ValueError) as error:
                raise ComputeError(
                    HelperErrorCode.CLUSTER_INVALID,
                    f"cluster {name!r} in {self.config_path} is invalid: {_invalid_keys(error)}",
                    self.config_path,
                ) from error
            path = self._rendered_path(name)
            text = render_toml(_slurm().operator_document(config))
            if self._files.read(path) != text:
                self._files.write(path, text)
            return path
        operator = self._operator_cluster()
        return self._operator_path if operator is not None and operator.name == name else None

    def test(self, cluster: SlurmClusterSettings) -> ClusterTestResult:
        """Whether ``cluster`` can be reached from here, its scheduler answers, and its workspace exists.

        Raises ``ComputeError`` (``cluster_invalid``) for settings the Slurm configuration rejects.
        """
        self._validated(cluster)
        reach = cluster.reach
        stage = ClusterTestStage.REACH
        if isinstance(reach, SshReach):
            alive = self._probe.run(
                ("ssh", *_SSH_OPTIONS, reach.host, "true"), timeout_seconds=_REACH_TIMEOUT_S
            )
            if alive is None or alive.returncode != 0:
                return _failed(
                    stage, alive.stderr if alive else None, f"could not reach {reach.host}"
                )
        sinfo = self._run(
            cluster,
            _SINFO if cluster.partition is None else (*_SINFO, "-p", cluster.partition),
            _SINFO_TIMEOUT_S,
        )
        stage = ClusterTestStage.SCHEDULER
        if sinfo is None or sinfo.returncode != 0:
            return _failed(stage, sinfo.stderr if sinfo else None, "sinfo did not answer")
        partitions, hardware = parse_sinfo(sinfo.stdout)
        if not partitions:
            where = "" if cluster.partition is None else f" for partition {cluster.partition}"
            return _failed(stage, None, f"sinfo lists no partition{where}")
        probe = self._run(
            cluster,
            ("test", "-d", cluster.workspace, "-a", "-w", cluster.workspace),
            _WORKSPACE_TIMEOUT_S,
        )
        if probe is None or probe.returncode != 0:
            detail = f"the workspace {cluster.workspace} is missing or not writable"
            return ClusterTestFailed(stage=ClusterTestStage.WORKSPACE, detail=detail[:_LINE_MAX])
        return ClusterTestPassed(hardware=hardware, partitions=partitions)

    def add(self, cluster: SlurmClusterSettings) -> ComputeSettings:
        """Save ``cluster`` once it passes its test; nothing is written otherwise.

        Raises ``ComputeError``: ``cluster_exists`` for a name already set up (in either file),
        ``cluster_invalid``, or ``cluster_test_failed`` with the test's own account.
        """
        self._validated(cluster)
        stored = self._stored_clusters()
        operator = self._operator_cluster()
        if cluster.name in stored or (operator is not None and operator.name == cluster.name):
            raise ComputeError(
                HelperErrorCode.CLUSTER_EXISTS,
                f"a cluster named {cluster.name!r} is already set up",
            )
        result = self.test(cluster)
        if isinstance(result, ClusterTestFailed):
            raise ComputeError(
                HelperErrorCode.CLUSTER_TEST_FAILED,
                f"{result.stage.value}: {result.detail}",
            )
        self._save({**stored, cluster.name: cluster})
        return self.settings()

    def remove(self, name: str) -> ComputeSettings:
        """Forget the cluster ``name`` of the host's file and its rendered operator file.

        Raises ``ComputeError``: ``unknown_cluster``, or ``cluster_read_only`` for the cluster
        of the operator file, which only the operator edits.
        """
        stored = self._stored_clusters()
        if name not in stored:
            operator = self._operator_cluster()
            if operator is not None and operator.name == name:
                raise ComputeError(
                    HelperErrorCode.CLUSTER_READ_ONLY,
                    f"cluster {name!r} comes from {self._operator_path}, which this helper does not edit",
                    self._operator_path,
                )
            raise ComputeError(
                HelperErrorCode.UNKNOWN_CLUSTER, f"no cluster named {name!r} is set up"
            )
        self._save({key: each for key, each in stored.items() if key != name})
        return self.settings()

    def _validated(self, cluster: SlurmClusterSettings) -> None:
        try:
            _slurm().slurm_config_of(cluster)
        except (ValidationError, ValueError) as error:
            raise ComputeError(
                HelperErrorCode.CLUSTER_INVALID,
                f"cluster {cluster.name!r} is invalid: {_invalid_keys(error)}",
            ) from error

    def _run(
        self, cluster: SlurmClusterSettings, argv: Sequence[str], timeout_s: float
    ) -> ProbeResult | None:
        """``argv`` on the cluster: over ssh from here, or here on a login node."""
        reach = cluster.reach
        command = (
            ("ssh", *_SSH_OPTIONS, reach.host, remote_command(argv))
            if isinstance(reach, SshReach)
            else tuple(argv)
        )
        return self._probe.run(command, timeout_seconds=timeout_s)

    def _stored_clusters(self) -> dict[str, SlurmClusterSettings]:
        path = self.config_path
        try:
            text = self._files.read(path)
        except OSError as error:
            raise ComputeError(
                HelperErrorCode.COMPUTE_CONFIG_UNREADABLE,
                f"{path} cannot be read: {error.strerror}",
                path,
            ) from error
        if text is None:
            return {}
        try:
            stored = _ComputeFile.model_validate(tomllib.loads(text))
            return {
                name: SlurmClusterSettings.model_validate({**table, "name": name})
                for name, table in stored.slurm.items()
            }
        except (tomllib.TOMLDecodeError, UnicodeDecodeError, ValidationError) as error:
            detail = _invalid_keys(error) if isinstance(error, ValidationError) else str(error)
            raise ComputeError(
                HelperErrorCode.COMPUTE_CONFIG_UNREADABLE,
                f"{path} is not valid: {detail}"[:_LINE_MAX],
                path,
            ) from error

    def _operator_cluster(self) -> SlurmClusterSettings | None:
        """The cluster of the operator ``slurm.toml``, or ``None`` when absent or not listable."""
        try:
            text = self._files.read(self._operator_path)
            table = None if text is None else tomllib.loads(text).get("slurm")
            if not isinstance(table, dict):
                return None
            return _slurm().operator_cluster(table)
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError, ValidationError, ValueError):
            return None

    def _save(self, clusters: Mapping[str, SlurmClusterSettings]) -> None:
        document: dict[str, object] = {
            "version": _FORMAT_VERSION,
            "slurm": {name: _stored(cluster) for name, cluster in sorted(clusters.items())},
        }
        self._files.write(self.config_path, render_toml(document))


def remote_command(argv: Sequence[str]) -> str:
    """``argv`` as one command line for the remote shell: each argument single-quoted."""
    return " ".join("'" + argument.replace("'", "'\\''") + "'" for argument in argv)


def _failed(stage: ClusterTestStage, stderr: str | None, fallback: str) -> ClusterTestFailed:
    first = next((line.strip() for line in (stderr or "").splitlines() if line.strip()), fallback)
    return ClusterTestFailed(stage=stage, detail=first[:_LINE_MAX])


__all__ = [
    "COMPUTE_FILE",
    "SLURM_FILE",
    "ConfigFiles",
    "MachineCompute",
    "accelerator_of_gpu_model",
    "parse_sinfo",
    "remote_command",
    "render_toml",
]


class OsConfigFiles:
    """The machine's configuration files on this filesystem (``ConfigFiles``)."""

    def read(self, path: str) -> str | None:
        """The file's text, or ``None`` when it does not exist."""
        try:
            return Path(path).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def write(self, path: str, text: str) -> None:
        """Replace the file whole: write a sibling, then rename it over the file."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            staged.write_text(text, encoding="utf-8")
            staged.replace(target)
        finally:
            staged.unlink(missing_ok=True)
