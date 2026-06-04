"""Run environment assembly over the existing sandbox implementations.

Layering:

    loop -> _RunContext -> RunEnvironment -> ComputeBackendImpl.make_sandbox -> Sandbox

``_RunContext`` owns the experiment lifecycle: workspace/log setup, reference
and helper file materialization, model construction, git snapshots, GPU
monitoring, and agent runner wiring.  It asks this module for a run-environment
session once the workspace is ready.

``RunEnvironment`` owns run-level execution policy for a location such as local,
Docker, or Modal.  It decides path exposure, bind mounts, execution constraints,
model-weight handling, prompt-visible paths, sandbox startup, and cleanup.  It
does not execute agent commands directly.

``ComputeBackendImpl.make_sandbox`` is the compute-platform factory.  It knows
how to construct a local/Docker/Modal sandbox for CUDA, Metal, or another
compute backend.

The concrete sandbox classes are still the command-execution abstraction.  They
run shell commands, read/write files, translate virtual paths, and manage the
container or remote process lifetime at the command layer.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Protocol

from deepagents.backends.sandbox import BaseSandbox

from vibe_serve.backends import ModalOptions, SandboxKind
from vibe_serve.backends.base import ComputeBackendImpl, SetupFn
from vibe_serve.constants import DEFAULT_AGENT_BACKEND, PROJECT_ROOT


@dataclass(frozen=True)
class RunEnvironmentSpec:
    """Run-environment selection at the CLI/config boundary.

    Environment-specific knobs stay inside ``options`` and are parsed only by the
    concrete environment selected by ``name``.
    """

    name: str = "local"
    options: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentPaths:
    """Paths as agents should refer to them inside the active environment."""

    acc_checker: str | None = None
    bench: str | None = None
    nsys_profiler: str | None = None
    torch_profiler: str | None = None


@dataclass(frozen=True)
class RunEnvironmentView:
    """Run-environment-neutral facts consumed by loops and agent construction."""

    paths: AgentPaths
    prompt_notes: str = ""
    isolated: bool = False
    cli_sandboxed: bool = False
    # When True, ``cli_sandboxed`` refers to a Modal sandbox (rather than a
    # local Docker container).  ``build_agent_runner`` treats Modal CLI
    # sandboxes specially (different executor, no per-invocation
    # workspace_arg).
    cli_modal_sandboxed: bool = False
    host_device_reselect: bool = True
    # Coarse environment label for prompt-template branching:
    # ``"local"`` | ``"docker"`` | ``"modal"``.
    env_kind: str = "local"


@dataclass(frozen=True)
class RunEnvironmentRequest:
    log_dir: Path
    workspace: Path
    ref_dir: Path | None
    backend: ComputeBackendImpl
    agent_backend: str | None
    cli_provider: str | None
    acc_checker_path: str | None = None
    bench_path: str | None = None
    nsys_profiler_path: str | None = None
    torch_profiler_path: str | None = None
    log: Callable[[str], None] | None = None
    project_root: Path = PROJECT_ROOT


class RunEnvironmentSession(Protocol):
    sandbox: BaseSandbox
    view: RunEnvironmentView

    def __enter__(self) -> "RunEnvironmentSession": ...
    def __exit__(self, exc_type: object, exc: object, tb: object) -> None: ...
    def close(self) -> None: ...


class RunEnvironment(Protocol):
    isolated: bool
    materialize_local_model_weights: bool
    default_profiler_kind: str
    backend_image: str | None

    def open(self, request: RunEnvironmentRequest) -> RunEnvironmentSession: ...
    def repair_workspace(self, workspace: Path, *, backend: ComputeBackendImpl, log: Callable[[str], None]) -> None: ...
    def remove_workspace_child(self, workspace: Path, rel_path: str, *, backend: ComputeBackendImpl) -> bool: ...


class _NoopWorkspaceRecovery:
    def repair_workspace(self, workspace: Path, *, backend: ComputeBackendImpl, log: Callable[[str], None]) -> None:
        return

    def remove_workspace_child(self, workspace: Path, rel_path: str, *, backend: ComputeBackendImpl) -> bool:
        return False


@dataclass
class _DefaultRunEnvironmentSession:
    sandbox: BaseSandbox
    view: RunEnvironmentView
    stop_on_close: bool = False
    _closed: bool = False

    def __enter__(self) -> "_DefaultRunEnvironmentSession":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.stop_on_close and hasattr(self.sandbox, "stop"):
            self.sandbox.stop()


class LocalEnvironment(_NoopWorkspaceRecovery):
    isolated = False
    materialize_local_model_weights = True
    default_profiler_kind = "nsys"
    backend_image = None

    def open(self, request: RunEnvironmentRequest) -> RunEnvironmentSession:
        sandbox = request.backend.make_sandbox(
            SandboxKind.LOCAL,
            host_workspace=str(request.workspace),
            log_path=None,
            bind_mounts=[],
            passthrough_paths=[],
            extra_env={},
            extra_init_commands=[],
        )
        return _DefaultRunEnvironmentSession(
            sandbox=sandbox,
            view=RunEnvironmentView(
                paths=AgentPaths(
                    acc_checker=request.acc_checker_path,
                    bench=request.bench_path,
                    nsys_profiler=request.nsys_profiler_path,
                    torch_profiler=request.torch_profiler_path,
                ),
            ),
            stop_on_close=False,
        )


@dataclass(frozen=True)
class DockerEnvironmentConfig:
    image: str | None = None


class DockerEnvironment:
    isolated = True
    materialize_local_model_weights = True
    default_profiler_kind = "nsys"

    def __init__(self, config: DockerEnvironmentConfig) -> None:
        self.config = config
        self.backend_image = config.image

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> "DockerEnvironment":
        image = options.get("image")
        return cls(DockerEnvironmentConfig(image=str(image) if image else None))

    def open(self, request: RunEnvironmentRequest) -> RunEnvironmentSession:
        bind_mounts, docker_symlinks, model_path = _container_mount_plan(request)
        extra_init_commands, cli_provider_env = _cli_container_setup(request)
        bind_mounts = _dedupe_mounts(bind_mounts)
        passthrough = ["/model"] if model_path is not None else []
        setup_fns = _symlink_setup_fns(docker_symlinks)

        sandbox = request.backend.make_sandbox(
            SandboxKind.DOCKER,
            host_workspace=str(request.workspace),
            log_path=request.log_dir / "docker.log",
            bind_mounts=bind_mounts,
            passthrough_paths=passthrough,
            extra_env=cli_provider_env,
            extra_init_commands=extra_init_commands,
            setup_fns=setup_fns,
        )
        log = request.log or (lambda _: None)
        label = getattr(request.backend, "image", self.config.image or "<backend-default>")
        log(f"[docker] starting container with image {label}")
        sandbox.start()

        return _DefaultRunEnvironmentSession(
            sandbox=sandbox,
            view=RunEnvironmentView(
                paths=_isolated_paths(request),
                prompt_notes=(
                    "Commands run inside the active execution environment. "
                    "Use normal shell commands to start, stop, and test the server."
                ),
                isolated=True,
                cli_sandboxed=True,
                env_kind="docker",
            ),
            stop_on_close=True,
        )

    def repair_workspace(self, workspace: Path, *, backend: ComputeBackendImpl, log: Callable[[str], None]) -> None:
        """Chown workspace files back to the host user after Docker writes."""
        if not workspace.exists():
            return
        uid, gid = os.getuid(), os.getgid()
        chown_cmd = f"chown -R {uid}:{gid} /workspace"
        try:
            result = _docker_workspace_run(
                workspace,
                backend=backend,
                shell_command=chown_cmd,
                timeout=120,
            )
            if result.returncode != 0:
                log(
                    f"[warn] chown failed for {workspace} "
                    f"(rc={result.returncode}): "
                    f"{result.stderr.decode(errors='replace').strip()}"
                )
        except Exception as exc:
            log(f"[warn] chown failed for {workspace}: {exc}")

    def remove_workspace_child(self, workspace: Path, rel_path: str, *, backend: ComputeBackendImpl) -> bool:
        target = workspace / rel_path
        try:
            _docker_workspace_run(
                workspace,
                backend=backend,
                shell_command=f"rm -rf -- {shlex.quote(f'/workspace/{rel_path}')}",
                timeout=120,
            )
        except Exception:
            pass
        return not (target.exists() or target.is_symlink())


@dataclass(frozen=True)
class ModalEnvironmentConfig:
    image: str | None = None
    gpu: str = "H100"
    model_volume: str | None = None
    app: str = "vibeserve"


class ModalEnvironment(_NoopWorkspaceRecovery):
    isolated = True
    materialize_local_model_weights = False
    default_profiler_kind = "torch"

    def __init__(self, config: ModalEnvironmentConfig) -> None:
        self.config = config
        self.model_volume: str | None = config.model_volume
        self.backend_image = config.image

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> "ModalEnvironment":
        return cls(
            ModalEnvironmentConfig(
                image=str(options["image"]) if options.get("image") else None,
                gpu=str(options.get("gpu") or "H100"),
                model_volume=(
                    str(options["model_volume"])
                    if options.get("model_volume")
                    else None
                ),
                app=str(options.get("app") or "vibeserve"),
            )
        )

    def open(self, request: RunEnvironmentRequest) -> RunEnvironmentSession:
        """Open the Modal-via-Docker run environment.

        Architecture (refactor April 2026): the agent (codex CLI) runs inside
        a *local* Docker container that does file editing only.  GPU-bound
        execution dispatches to Modal via ``modal run main.py::<function>``
        calls the implementer-authored code makes; we install the Modal
        Python SDK and mount the host's ``~/.modal.toml`` into the container
        so those calls authenticate.

        We retain the host-side Modal Volume bootstrap (model + optional
        draft) so the implementer's ``modal.Volume.from_name(...)`` calls
        resolve.  The previous "long-lived Modal sandbox running codex
        inside" architecture is gone — it caused HOME-leak auth bugs,
        codex-vs-model-weight memory contention, and per-run sandbox
        cold-start overhead that this design eliminates.
        """
        # Host-side: ensure Modal Volumes exist for the model + optional
        # draft.  These run before the Docker container starts and are
        # idempotent (skip-if-ready sentinel).
        self._ensure_model_volume(request)
        self._ensure_draft_volume(request)

        bind_mounts, docker_symlinks, _model_path = _container_mount_plan(request)
        extra_init_commands, cli_provider_env = _cli_container_setup(request)

        # Mount host Modal auth so `modal run` inside the container
        # authenticates as the host user.
        modal_auth = Path.home() / ".modal.toml"
        if modal_auth.exists():
            bind_mounts.append((str(modal_auth), "/root/.modal.toml", True))
        modal_config_dir = Path.home() / ".modal"
        if modal_config_dir.is_dir():
            bind_mounts.append((str(modal_config_dir), "/root/.modal", True))

        # Install the Modal Python SDK alongside the agent's other packages.
        # Pinned to a recent release; the wire protocol is forward-compatible
        # with the host's Modal CLI as long as both are within ~one major.
        extra_init_commands.insert(0, "pip install --quiet 'modal>=0.66'")

        bind_mounts = _dedupe_mounts(bind_mounts)
        setup_fns = _symlink_setup_fns(docker_symlinks)

        sandbox = request.backend.make_sandbox(
            SandboxKind.DOCKER,
            host_workspace=str(request.workspace),
            log_path=request.log_dir / "docker.log",
            bind_mounts=bind_mounts,
            passthrough_paths=[],  # weights live in Modal Volumes, not on host
            extra_env=cli_provider_env,
            extra_init_commands=extra_init_commands,
            setup_fns=setup_fns,
        )
        log = request.log or (lambda _: None)
        log(
            "[modal] starting local Docker editor; GPU work will dispatch "
            "to Modal via `modal run main.py::<function>`"
        )
        sandbox.start()

        app_name = _modal_app_name(request.workspace, fallback=self.config.app)
        return _DefaultRunEnvironmentSession(
            sandbox=sandbox,
            view=RunEnvironmentView(
                paths=_isolated_paths(request),
                prompt_notes=_modal_runtime_notes(self.config.gpu, app_name),
                isolated=True,
                cli_sandboxed=True,
                cli_modal_sandboxed=False,  # codex no longer runs inside Modal
                host_device_reselect=False,
                env_kind="modal",
            ),
            stop_on_close=True,
        )

    def _ensure_model_volume(self, request: RunEnvironmentRequest) -> None:
        if self.model_volume or request.ref_dir is None:
            return
        meta_path = request.ref_dir / "meta.json"
        if not meta_path.exists():
            return
        from vibe_serve.sandbox.modal_model_setup import ensure_model_volume

        meta = json.loads(meta_path.read_text())
        model_id = meta.get("model_id")
        if not model_id:
            raise ValueError(
                f"meta.json at {meta_path} missing required 'model_id' field "
                "(needed for Modal auto-upload)"
            )
        hf_available = bool(
            os.environ.get("HF_TOKEN")
            or os.environ.get("HUGGING_FACE_HUB_TOKEN")
        )
        local_model = request.ref_dir / "model"
        local_path = None
        if not hf_available and local_model.exists() and local_model.resolve().is_dir():
            local_path = str(local_model.resolve())
        self.model_volume = ensure_model_volume(
            model_id,
            revision=meta.get("revision"),
            local_path=local_path,
            log=request.log or print,
        )

    def _ensure_draft_volume(
        self, request: RunEnvironmentRequest,
    ) -> str | None:
        """Auto-provision a Modal Volume for an auxiliary draft model.

        EAGLE3-style speculative decoding wants a draft model alongside the
        target weights.  When ``draft_meta.json`` sits next to ``meta.json``,
        upload it to its own Modal Volume and return the name so the sandbox
        can mount it read-only at ``/draft_model``.
        """
        if request.ref_dir is None:
            return None
        draft_meta_path = request.ref_dir / "draft_meta.json"
        if not draft_meta_path.exists():
            return None
        from vibe_serve.sandbox.modal_model_setup import ensure_model_volume

        draft_meta = json.loads(draft_meta_path.read_text())
        draft_model_id = draft_meta.get("model_id")
        if not draft_model_id:
            raise ValueError(
                f"draft_meta.json at {draft_meta_path} missing required "
                "'model_id' field"
            )
        hf_available = bool(
            os.environ.get("HF_TOKEN")
            or os.environ.get("HUGGING_FACE_HUB_TOKEN")
        )
        local_draft = request.ref_dir / "draft_model"
        local_path = None
        if (
            not hf_available
            and local_draft.exists()
            and local_draft.resolve().is_dir()
        ):
            local_path = str(local_draft.resolve())
        return ensure_model_volume(
            draft_model_id,
            revision=draft_meta.get("revision"),
            local_path=local_path,
            log=request.log or print,
        )


def build_run_environment(spec: RunEnvironmentSpec) -> RunEnvironment:
    if spec.name == "local":
        return LocalEnvironment()
    if spec.name == "docker":
        return DockerEnvironment.from_options(spec.options)
    if spec.name == "modal":
        return ModalEnvironment.from_options(spec.options)
    raise ValueError(f"unknown run environment: {spec.name!r}")


def make_run_environment_spec(
    *,
    use_docker: bool = False,
    docker_image: str | None = None,
    use_modal: bool = False,
    modal_gpu: str = "H100",
    modal_model_volume: str | None = None,
    modal_app: str = "vibeserve",
) -> RunEnvironmentSpec:
    """Build a spec from the current CLI compatibility flags.

    Modal mode (April 2026 refactor) runs the agent in a *local Docker
    container* and dispatches GPU work via ``modal run main.py::<function>``,
    so the legacy long-lived-Modal-sandbox knobs (timeout / idle_timeout)
    no longer apply here — they live on the implementer's per-function
    ``@app.function(timeout=...)`` / ``@app.cls(container_idle_timeout=...)``
    decorators instead.
    """
    if use_docker and use_modal:
        raise ValueError("--docker and --modal are mutually exclusive")
    name = "modal" if use_modal else ("docker" if use_docker else "local")
    return RunEnvironmentSpec(
        name=name,
        options={
            "image": docker_image,
            "gpu": modal_gpu,
            "model_volume": modal_model_volume,
            "app": modal_app,
        },
    )


def _modal_app_name(workspace: Path, fallback: str) -> str:
    """Derive a Modal app name unique to this run.

    Two concurrent runs must not share a ``modal.App(name=...)`` — Modal
    treats them as the same app, so the second run's deploys/lookups can
    clobber or shadow the first.  ``exp_dir.name`` is already unique per
    run (timestamp + exp_name) and is the natural source.  We sanitize it
    to Modal's allowed alphabet (lowercase alphanumerics + hyphens, ≤63
    chars) and prefix with ``vibeserve-`` so all app names are findable.
    """
    candidate = workspace.parent.name or fallback or "vibeserve"
    sanitized = "".join(c if c.isalnum() or c == "-" else "-" for c in candidate.lower())
    sanitized = "-".join(part for part in sanitized.split("-") if part)
    name = f"vibeserve-{sanitized}" if sanitized else "vibeserve"
    return name[:63].rstrip("-") or "vibeserve"


def _modal_runtime_notes(gpu: str, app_name: str) -> str:
    """Render the Modal-mode runtime instructions for agent prompts.

    Kept task-agnostic: doesn't name specific model IDs, volume names, or
    mount points — those are workload-specific and the implementer reads
    them from the input metadata files (``reference/meta.json`` etc.) at
    setup time.  Pre-staged Modal Volumes follow the framework's
    ``vibeserve-model-<normalized-model-id>`` convention; the implementer
    can derive the volume name from the ``model_id`` in those metadata
    files (or just call ``modal.Volume.from_name(name)`` once it knows it).
    """
    return (
        "Execution model — read carefully:\n"
        "  - You are inside a *local* Docker container for editing "
        "and lightweight testing only. The container does NOT have a "
        "GPU attached.\n"
        f"  - **Per-run namespace prefix (REQUIRED, do not change)**: "
        f"this run's unique Modal namespace prefix is `{app_name}`. Use "
        "it on every Modal-namespace name you create so concurrent runs "
        "do not clobber each other's deploys, web endpoints, dicts, "
        "queues, or auxiliary volumes:\n"
        f"      • App: `app = modal.App({app_name!r})`\n"
        f"      • Web endpoint labels (if any): "
        f"`@modal.fastapi_endpoint(label=\"{app_name}-<purpose>\")` — "
        "without a unique label two runs collide on the same public URL "
        "and the second deploy overwrites the first.\n"
        f"      • Auxiliary Volumes / Dicts / Queues / Secrets you "
        f"create: prefix the name with `{app_name}-` "
        f"(e.g. `modal.Volume.from_name(\"{app_name}-traces\", "
        "create_if_missing=True)`).\n"
        "    Model-weight Volumes that the framework pre-stages "
        "(named `vibeserve-model-<...>`, see below) are intentionally "
        "*shared* across runs — never rename those, never recreate them "
        "under the per-run prefix.\n"
        "  - All GPU-bound work (model loading, attention forwards, "
        "benchmarking, profiling) must run on Modal. Structure the "
        "implementation around `modal.App`: define the server as "
        f"`@app.cls(image=..., gpu={gpu!r}, volumes={{...}})` with "
        "`@modal.enter()` for model load and `@modal.method()` for "
        "inference. Define benchmark / profile entry points as "
        f"`@app.function(image=..., gpu={gpu!r}, volumes=...)`.\n"
        "  - To run any GPU work: `modal run main.py::<function>`. "
        "The Modal CLI is installed and authenticated (`~/.modal.toml` "
        "is mounted from the host).\n"
        "  - Model weights are pre-staged in Modal Volumes by the "
        "framework before this round started. Read the model metadata "
        "files in your reference/input directory (typically "
        "`reference/meta.json` for the primary model and "
        "`reference/draft_meta.json` for any speculative-decoding draft "
        "model when applicable) to learn the `model_id` for each. "
        "The framework normalizes each `model_id` into the volume "
        "name with this exact rule (matches "
        "`vibe_serve/modal_model_setup.py::_volume_name_for`):\n"
        "      `re.sub(r\"[^a-z0-9]+\", \"-\", model_id.lower()).strip(\"-\")`\n"
        "    prefixed with `vibeserve-model-`. Every run of non-"
        "alphanumeric characters (slashes, dots, underscores, etc.) "
        "collapses to a single `-`. So `org/Foo-1.2-X` becomes "
        "`vibeserve-model-org-foo-1-2-x` (the dot in `1.2` becomes a "
        "dash, not preserved). When in doubt run `modal volume list` "
        "to see the actual names the framework provisioned. "
        "Use `modal.Volume.from_name(<that-name>)` and mount it at "
        "whatever container path you prefer (no fixed convention is "
        "required).\n"
        "  - Use `scaledown_window=120` on `@app.cls` so back-to-back "
        "benchmark calls reuse the warm container (KV cache, CUDA graphs, "
        "compiled grammars, etc. preserved between invocations within the "
        "warm window). Note: Modal renamed `container_idle_timeout` to "
        "`scaledown_window` (Feb 2025); the old name raises a deprecation "
        "error in current Modal SDK versions, so do NOT use it.\n"
        "  - Do NOT run `python main.py` to start a long-lived FastAPI "
        "server inside this container — there's no GPU here. Direct "
        "`Server.method.remote(...)` calls or `modal run main.py::<function>` "
        "are the testing interface.\n"
        "\n"
        "Profiling on Modal — REQUIRED entry point:\n"
        "  Profiling must run on the Modal GPU container, NOT in this "
        "editor container. The framework's profiler agent expects the "
        "following two symbols in `main.py` and will invoke the local "
        "entrypoint by name; without these, the profiler agent cannot "
        "capture real GPU traces and will fall back to synthetic data.\n"
        "\n"
        f"  1. `@app.function(image=..., gpu={gpu!r}, volumes=...)` "
        "called `profile_remote(num_iters, max_tokens, prompt)` — runs "
        "on Modal, wraps a representative steady-state workload "
        "(e.g. several `Server().generate.local(...)` calls or direct "
        "model.generate calls inside the function) in `torch.profiler."
        "profile(activities=[CPU, CUDA])`, summarizes the captured "
        "events into the JSON schema documented at "
        "`torch_profiler/analyze_torch_profile.py` (the schema in the "
        "module docstring: `{version, captured_at, mode, device, dtype, "
        "num_iters, total_cuda_time_us, total_cpu_time_us, events: "
        "[{name, category, cpu_time_us, cuda_time_us, self_cpu_time_us, "
        "self_cuda_time_us, count}, ...]}`), and **returns the dict**.\n"
        "  2. `@app.local_entrypoint()` called `modal_profile(output: "
        "str = '/workspace/prof.json', num_iters: int = 20, max_tokens: "
        "int = 32, prompt: str = 'The capital of France is')` — calls "
        "`profile_remote.remote(...)` and writes the returned dict as "
        "JSON to `output` so the analyzer subcommands "
        "(`tables`, `kernels`, `summary`, …) can read it.\n"
        "\n"
        "  Reference for the JSON shape: copy the `_summarize_prof` "
        "helper from `torch_profiler/analyze_torch_profile.py`. The "
        "minimum acceptable summarizer is:\n"
        "      from torch.autograd import DeviceType\n"
        "      def _summarize(prof, num_iters):\n"
        "          totals = prof.key_averages()\n"
        "          events, total_cuda, total_cpu = [], 0.0, 0.0\n"
        "          for ev in totals:\n"
        "              cuda_us = float(getattr(ev, 'device_time_total', 0.0) or 0.0)\n"
        "              cpu_us = float(getattr(ev, 'cpu_time_total', 0.0) or 0.0)\n"
        "              self_cuda = float(getattr(ev, 'self_device_time_total', 0.0) or 0.0)\n"
        "              self_cpu = float(getattr(ev, 'self_cpu_time_total', 0.0) or 0.0)\n"
        "              name = ev.key\n"
        "              if ev.device_type == DeviceType.CUDA or 'cuda' in name.lower() or (cuda_us > 0 and cpu_us < cuda_us / 4):\n"
        "                  cat = 'kernel'\n"
        "              elif name.startswith('aten::') or name.startswith('torch::'):\n"
        "                  cat = 'operator'\n"
        "              elif any(t in name.lower() for t in ('memcpy', 'memset', 'malloc', 'free')):\n"
        "                  cat = 'memory'\n"
        "              else:\n"
        "                  cat = 'cpu'\n"
        "              events.append({'name': name, 'category': cat,\n"
        "                  'cpu_time_us': cpu_us, 'cuda_time_us': cuda_us,\n"
        "                  'self_cpu_time_us': self_cpu, 'self_cuda_time_us': self_cuda,\n"
        "                  'count': int(ev.count)})\n"
        "              total_cuda += self_cuda; total_cpu += self_cpu\n"
        "          return {'version': 1, 'total_cuda_time_us': total_cuda,\n"
        "              'total_cpu_time_us': total_cpu, 'num_events': len(events),\n"
        "              'events': events, 'num_iters': num_iters}\n"
        "  Wrap your representative workload (a torch.profiler.schedule "
        "with wait/warmup/active is recommended; otherwise profile "
        "inside a plain `with torch.profiler.profile(...) as prof:` "
        "block followed by `torch.cuda.synchronize()`).\n"
        "  After the function returns, `modal_profile` should append "
        "`captured_at` (ISO-8601 UTC), `mode='model'`, `device='cuda'`, "
        "`dtype` (whatever was loaded), and `wall_time_sec` to the "
        "dict before writing to disk."
    )


def _isolated_paths(request: RunEnvironmentRequest) -> AgentPaths:
    return AgentPaths(
        acc_checker="acc_checker" if request.acc_checker_path else None,
        bench="bench" if request.bench_path else None,
        nsys_profiler="nsys_profiler" if request.nsys_profiler_path else None,
        torch_profiler="torch_profiler" if request.torch_profiler_path else None,
    )


def _docker_workspace_run(
    workspace: Path,
    *,
    backend: ComputeBackendImpl,
    shell_command: str,
    timeout: int,
) -> subprocess.CompletedProcess:
    image = getattr(backend, "image", "ubuntu:latest")
    return subprocess.run(
        [
            "docker", "run", "--rm",
            "-v", f"{workspace}:/workspace",
            image,
            "bash", "-c", shell_command,
        ],
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _container_mount_plan(
    request: RunEnvironmentRequest,
    *,
    include_cli_provider_mounts: bool = True,
) -> tuple[list[tuple[str, str, bool]], list[tuple[str, str]], Path | None]:
    """Build the bind mounts + setup symlinks for a sandbox.

    ``include_cli_provider_mounts`` controls whether CLI auth dirs and the
    full project tree are added under ``/root`` and ``/opt/vibeserve``.
    Defaults to True; both supported environments (local Docker and the
    Modal-via-Docker mode) bind-mount these directly.
    """
    bind_mounts: list[tuple[str, str, bool]] = []
    symlinks: list[tuple[str, str]] = []
    ref_dir = request.ref_dir

    skip_model_symlinks = {"model", "draft_model"}

    if ref_dir is not None:
        _collect_symlink_mounts(
            ref_dir,
            "/workspace/reference",
            bind_mounts=bind_mounts,
            symlinks=symlinks,
            skip=skip_model_symlinks,
        )
        if ref_dir.parent != ref_dir:
            _collect_symlink_mounts(
                ref_dir.parent,
                "/workspace",
                bind_mounts=bind_mounts,
                symlinks=symlinks,
                skip=skip_model_symlinks,
            )

    model_path: Path | None = None
    if ref_dir is not None:
        for candidate in (ref_dir / "model", ref_dir.parent / "model"):
            if candidate.is_symlink() or candidate.is_dir():
                model_path = candidate
                break
    if model_path is not None:
        resolved = model_path.resolve()
        host_path = _find_mount_root(resolved)
        if host_path == resolved:
            bind_mounts.append((str(host_path), "/model", True))
        else:
            rel = resolved.relative_to(host_path)
            ancestor_mount = "/workspace/_mounts/model"
            bind_mounts.append((str(host_path), ancestor_mount, True))
            symlinks.append(("/model", f"{ancestor_mount}/{rel}"))

    if ref_dir is not None:
        for candidate in (ref_dir / "draft_model", ref_dir.parent / "draft_model"):
            if candidate.is_symlink() or candidate.is_dir():
                resolved = candidate.resolve()
                host_path = _find_mount_root(resolved)
                if host_path == resolved:
                    bind_mounts.append((str(host_path), "/draft_model", True))
                else:
                    rel = resolved.relative_to(host_path)
                    ancestor_mount = "/workspace/_mounts/draft_model"
                    bind_mounts.append((str(host_path), ancestor_mount, True))
                    symlinks.append(("/draft_model", f"{ancestor_mount}/{rel}"))
                break

    if request.acc_checker_path:
        bind_mounts.append((request.acc_checker_path, "/workspace/acc_checker", True))
    if request.bench_path:
        bind_mounts.append((request.bench_path, "/workspace/bench", True))
    if request.nsys_profiler_path:
        bind_mounts.append((request.nsys_profiler_path, "/workspace/nsys_profiler", True))
    if request.torch_profiler_path:
        bind_mounts.append((request.torch_profiler_path, "/workspace/torch_profiler", True))

    if (
        include_cli_provider_mounts
        and (request.agent_backend or DEFAULT_AGENT_BACKEND) == "cli"
        and request.cli_provider
    ):
        from vibe_serve.agents.cli_docker import auth_bind_mounts

        bind_mounts.extend(auth_bind_mounts(request.cli_provider))
        bind_mounts.append((str(request.project_root), "/opt/vibeserve", True))

    return bind_mounts, symlinks, model_path


def _cli_container_setup(
    request: RunEnvironmentRequest,
) -> tuple[list[str], dict[str, str]]:
    effective_agent = request.agent_backend or DEFAULT_AGENT_BACKEND
    if effective_agent != "cli" or not request.cli_provider:
        return [], {}
    from vibe_serve.agents.cli_docker import (
        DOCKER_PROVIDER_ENV,
        docker_init_commands,
    )

    provider = request.cli_provider
    env = dict(DOCKER_PROVIDER_ENV.get(provider, {}))
    commands = docker_init_commands(provider)
    return commands, env


def _dedupe_mounts(
    mounts: list[tuple[str, str, bool]],
) -> list[tuple[str, str, bool]]:
    seen: dict[str, tuple[str, str, bool]] = {}
    for host_path, container_path, readonly in mounts:
        seen[container_path] = (host_path, container_path, readonly)
    return list(seen.values())


def _symlink_setup_fns(symlinks: list[tuple[str, str]]) -> list[SetupFn]:
    if not symlinks:
        return []
    symlink_cmds = [f"ln -sfn {target} {link}" for link, target in symlinks]

    def install_symlinks(sb: BaseSandbox) -> None:
        for cmd in symlink_cmds:
            sb.execute(cmd)
        if hasattr(sb, "save_symlink_commands"):
            sb.save_symlink_commands(symlink_cmds)

    return [install_symlinks]


def _collect_symlink_mounts(
    scan_dir: Path,
    container_prefix: str,
    *,
    bind_mounts: list[tuple[str, str, bool]],
    symlinks: list[tuple[str, str]],
    skip: set[str] | None = None,
) -> None:
    for child in scan_dir.iterdir():
        if not child.is_symlink():
            continue
        if skip and child.name in skip:
            continue
        target = child.resolve()
        try:
            target.relative_to(scan_dir.resolve())
        except ValueError:
            pass
        else:
            continue

        host_path = _find_mount_root(target)
        if host_path == target:
            bind_mounts.append((str(host_path), f"{container_prefix}/{child.name}", True))
        else:
            rel = target.relative_to(host_path)
            ancestor_mount = f"/workspace/_mounts/{child.name}"
            bind_mounts.append((str(host_path), ancestor_mount, True))
            symlinks.append((f"{container_prefix}/{child.name}", f"{ancestor_mount}/{rel}"))


def _find_mount_root(target: Path) -> Path:
    if not target.is_dir():
        return target
    needs_ancestor = False
    for path in target.rglob("*"):
        if path.is_symlink():
            link_target = path.parent / path.readlink()
            try:
                link_target.resolve().relative_to(target.resolve())
            except ValueError:
                needs_ancestor = True
                break
    if not needs_ancestor:
        return target
    root = target
    for path in target.rglob("*"):
        if path.is_symlink():
            resolved = (path.parent / path.readlink()).resolve()
            while not str(resolved).startswith(str(root)):
                root = root.parent
    return root
