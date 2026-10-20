"""Serve a comparison baseline's engine on a Slurm node, from a published container image.

A comparison baseline (vLLM, SGLang) is measured by the same executor, stages and benchmark
as a candidate; only the *service* differs. The operator's policy names the candidate's server
(``[vibesys.service]``); this module swaps it for the baseline's, run from its image with
Apptainer, and appends the baseline's own benchmark arguments.

The operator's per-machine settings (``[vibesys.comparison]``) say where images are cached and
where model weights live. The task never does: it names only a digest-pinned image. The first
job on a machine pulls the image into a ``.sif`` under the cache directory (serialized by a
lock, so concurrent jobs pull once) and every later job reuses it.
"""

from __future__ import annotations

import posixpath
import re
import shlex
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from vs_slurm.api import PORT_PLACEHOLDER, SlurmService

if TYPE_CHECKING:
    from vs_sandbox.slurm_policy import SlurmExecutionPolicy

SERVE_PORT_PLACEHOLDER = "${PORT}"
"""The placeholder a task's ``serve`` argv uses for the port the job assigns."""

_GPU_FLAGS = {"rocm": "--rocm", "nvidia": "--nv"}
_GPU_VISIBILITY_VARIABLES = ("ROCR_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "CUDA_VISIBLE_DEVICES")
_DEFAULT_READINESS_URL = f"http://127.0.0.1:{PORT_PLACEHOLDER}/v1/models"
_SCRATCH = ".vs-baseline-cache"
_ENV_NAME = r"^[A-Za-z_][A-Za-z0-9_]*$"


def _absolute_posix(value: str, field: str) -> str:
    if not value.startswith("/") or posixpath.normpath(value) != value:
        message = f"{field} must be an absolute normalized POSIX path, got {value!r}"
        raise ValueError(message)
    return value


class ComparisonMachine(BaseModel):
    """The ``[vibesys.comparison]`` table: what this machine provides for comparison baselines.

    A machine without the table measures no comparison and reports them "not configured".
    Nothing here is secret, and nothing here belongs in a task: the cache directories are this
    machine's, and ``env_passthrough`` names variables of the job's own environment (set by the
    operator's setup script) rather than holding their values.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    gpu: Literal["rocm", "nvidia"]
    """Which GPU runtime the container binds: ``--rocm`` or ``--nv``."""
    image_cache_dir: str
    """Where pulled ``.sif`` files and Apptainer's layer cache live; shared by the machine's jobs."""
    model_cache_dir: str
    """The Hugging Face home holding the model weights; mounted read-only, with hub access offline."""
    apptainer: str = Field(default="apptainer", min_length=1)
    binds: tuple[str, ...] = ()
    """Extra ``--bind`` specs (``src[:dst[:opts]]``), for a library or license directory."""
    env_passthrough: tuple[str, ...] = ()
    """Names of job environment variables forwarded into the container, for example a token."""

    @field_validator("binds", "env_passthrough", mode="before")
    @classmethod
    def _lists_are_tuples(cls, value: object) -> object:
        # The operator file is read in strict mode, where a TOML array is a list.
        return tuple(value) if isinstance(value, list) else value

    @field_validator("image_cache_dir")
    @classmethod
    def _image_cache_is_absolute(cls, value: str) -> str:
        return _absolute_posix(value, "image_cache_dir")

    @field_validator("model_cache_dir")
    @classmethod
    def _model_cache_is_absolute(cls, value: str) -> str:
        return _absolute_posix(value, "model_cache_dir")

    @field_validator("apptainer")
    @classmethod
    def _apptainer_is_a_program(cls, value: str) -> str:
        if " " in value or ("/" in value and not value.startswith("/")):
            message = "apptainer must be a program name or an absolute path"
            raise ValueError(message)
        return value

    @field_validator("env_passthrough")
    @classmethod
    def _passthrough_names_variables(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        bad = [name for name in value if re.fullmatch(_ENV_NAME, name) is None]
        if bad:
            message = f"env_passthrough must name environment variables, got {bad!r}"
            raise ValueError(message)
        return value

    @field_validator("binds")
    @classmethod
    def _binds_are_specs(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not spec or "\n" in spec for spec in value):
            message = "binds must be non-empty single-line bind specs"
            raise ValueError(message)
        return value


class BaselineServer(BaseModel):
    """What to serve for one comparison: the pinned image, the argv and the engine's environment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    image: str = Field(pattern=r"^docker://\S+@sha256:[0-9a-f]{64}$")
    argv: tuple[str, ...] = Field(min_length=1)
    """The server's argv with ``${PORT}``; run inside the image."""
    env: dict[str, str] = Field(default_factory=dict)
    ready_timeout_seconds: int = Field(gt=0)
    benchmark_args: tuple[str, ...] = ()

    @property
    def digest(self) -> str:
        """The image's sha256 hex digest, which names its cached ``.sif``."""
        return self.image.rsplit("@sha256:", 1)[1]


def _with_port(element: str) -> str:
    """Quote *element* for the script, with every ``${PORT}`` expanding the job's port."""
    return '"$1"'.join(shlex.quote(part) for part in element.split(SERVE_PORT_PLACEHOLDER))


def serve_script(machine: ComparisonMachine, server: BaselineServer) -> str:
    """The POSIX shell script that pulls the image once and replaces itself with the server.

    ``$1`` is the port. Every value is quoted for the shell, so nothing a task or an operator
    wrote can end a word early.
    """
    cache = shlex.quote(machine.image_cache_dir)
    sif = shlex.quote(f"{machine.image_cache_dir}/{server.digest}.sif")
    lock = shlex.quote(f"{machine.image_cache_dir}/{server.digest}.lock")
    apptainer = shlex.quote(machine.apptainer)
    scratch = f'"$PWD"/{_SCRATCH}'
    environment: dict[str, str] = {
        "HF_HOME": machine.model_cache_dir,
        "HF_HUB_OFFLINE": "1",
        "VLLM_CACHE_ROOT": f"{scratch}/vllm",
        "TRITON_CACHE_DIR": f"{scratch}/triton",
        "XDG_CACHE_HOME": f"{scratch}/xdg",
        "TORCHINDUCTOR_CACHE_DIR": f"{scratch}/inductor",
        **server.env,
    }
    lines = [
        "set -eu",
        f"mkdir -p {cache}",
        f"if [ ! -f {sif} ]; then",
        "  (",
        "    flock 9",
        f"    if [ ! -f {sif} ]; then",
        # Built on the node's own scratch: some shared filesystems lack xattr support, and
        # building a .sif there stalls in mksquashfs. Only the finished file lands in the cache.
        f'      work="${{TMPDIR:-{cache}/work}}/vs-apptainer-$$"',
        '      mkdir -p "$work"',
        '      export APPTAINER_CACHEDIR="$work/layers" APPTAINER_TMPDIR="$work"',
        '      mkdir -p "$APPTAINER_CACHEDIR"',
        f'      {apptainer} pull "$work/image.sif" {shlex.quote(server.image)}',
        f'      cp "$work/image.sif" {sif}.$$',
        f"      mv {sif}.$$ {sif}",
        '      rm -rf "$work"',
        "    fi",
        f"  ) 9>{lock}",
        "fi",
        f"mkdir -p {scratch}/vllm {scratch}/triton {scratch}/xdg {scratch}/inductor",
    ]
    for name, value in environment.items():
        # The cache variables hold a "$PWD"-relative path, which the shell must expand.
        text = value if value.startswith(f"{scratch}/") else shlex.quote(value)
        lines.append(f"export APPTAINERENV_{name}={text}")
    lines.extend(
        f'if [ -n "${{{name}+x}}" ]; then export APPTAINERENV_{name}="${name}"; fi'
        for name in (*_GPU_VISIBILITY_VARIABLES, *machine.env_passthrough)
    )
    binds = (f"{machine.model_cache_dir}:{machine.model_cache_dir}:ro", *machine.binds)
    bind_flags = " ".join(f"--bind {shlex.quote(spec)}" for spec in binds)
    serve = " ".join(_with_port(element) for element in server.argv)
    lines.append(
        f"exec {apptainer} exec --cleanenv {_GPU_FLAGS[machine.gpu]} {bind_flags} "
        f'--bind "$PWD":"$PWD" {sif} {serve}'
    )
    return "\n".join(lines) + "\n"


def serve_command(machine: ComparisonMachine, server: BaselineServer) -> tuple[str, ...]:
    """The service argv for a Slurm job: the script, run by ``sh``, taking the job's port."""
    script = serve_script(machine, server)
    if PORT_PLACEHOLDER in script:
        message = (
            f"a serve argument may not contain {PORT_PLACEHOLDER}; use {SERVE_PORT_PLACEHOLDER}"
        )
        raise ValueError(message)
    return ("sh", "-c", script, "vs-baseline", PORT_PLACEHOLDER)


class ComparisonNotConfiguredError(ValueError):
    """The operator's policy cannot measure a comparison; the message says what is missing."""


def comparison_policy(
    base: SlurmExecutionPolicy,
    machine: ComparisonMachine,
    server: BaselineServer,
) -> SlurmExecutionPolicy:
    """*base* with the baseline's server in place of the candidate's.

    Everything else of the operator's policy stays: the setup script, the evaluator arguments
    that point at the server's port, and the ledger. Raises ``ComparisonNotConfiguredError``
    when *base* has no service, since its evaluator arguments then cannot name a port.
    """
    if base.service is None:
        message = (
            "the operator file has no [vibesys.service], so no server's port reaches the benchmark"
        )
        raise ComparisonNotConfiguredError(message)
    service = SlurmService(
        command=serve_command(machine, server),
        readiness_url=base.service.readiness_url or _DEFAULT_READINESS_URL,
        startup_timeout_seconds=server.ready_timeout_seconds,
    )
    return base.model_copy(
        update={
            "service": service,
            # Jobs shared through the evaluation ledger are keyed by a run and its candidates;
            # a comparison belongs to neither.
            "ledger": None,
            "benchmark_arguments": (*base.benchmark_arguments, *server.benchmark_args),
        }
    )
