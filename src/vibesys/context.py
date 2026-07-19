"""Shared run context: ``_RunContext``, ``create_run_context``, and ``setup_exp_dir``."""

import shutil
import subprocess
import uuid
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO, TypeVar, overload

from pydantic import BaseModel

from vibesys import backends
from vibesys.agents import build_agent_runner
from vibesys.agents.base import AgentRunner
from vibesys.agents.progress import AgentProgress
from vibesys.backends.base import ComputeBackendImpl, ContentionMonitor
from vibesys.config import Config, as_config
from vibesys.constants import (
    DEFAULT_AGENT_BACKEND,
    DEFAULT_COMPUTE_BACKEND,
    PROJECT_ROOT,
    ComputeBackend,
)
from vibesys.domains.base import DomainName
from vibesys.domains.environment import (
    EnvironmentContext,
    EnvironmentHooks,
    EnvironmentPatch,
    NoopEnvironmentHooks,
)
from vibesys.errors import ConfigurationDiagnostic, ConfigurationError
from vibesys.llm_client import build_model
from vibesys.profilers import (
    ACTIVE_PROFILER_KINDS,
    ProfilerKind,
    preflight_profiler_kind,
    profiler_definition,
    resolve_profiler_kind,
)
from vibesys.render import HeadlessRenderer, output_sink
from vibesys.run import DeviceLease, GitTracker, RunCommands, RunLogger, RunPaths, Workspace
from vibesys.sandbox.run_environment import (
    RunEnvironment,
    RunEnvironmentRequest,
    RunEnvironmentSession,
    RunEnvironmentSpec,
    build_run_environment,
    make_run_environment_spec,
)

if TYPE_CHECKING:
    from vibesys.server.supervisor import RunSupervisor

T = TypeVar("T", bound=BaseModel)


def setup_exp_dir(
    exp_name: str,
    project_root: Path = PROJECT_ROOT,
    existing: bool = False,
) -> Path:
    """Create or validate exp_env/<timestamp>-<exp_name>/ directory with git init."""
    if existing:
        exp_dir = project_root / "exp_env" / exp_name
    else:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        exp_dir = project_root / "exp_env" / f"{timestamp}-{uuid.uuid4().hex[:8]}-{exp_name}"
    if existing:
        if not exp_dir.is_dir():
            raise FileNotFoundError(f"Experiment directory not found: {exp_dir}")
        return exp_dir
    exp_dir.mkdir(parents=True, exist_ok=False)
    if not (exp_dir / ".git").is_dir():
        subprocess.run(
            ["git", "init"],
            cwd=exp_dir,
            capture_output=True,
            check=True,
        )
    return exp_dir


def _coerce_dir(raw: str | Path | None, label: str) -> Path | None:
    if raw is None:
        return None
    p = Path(raw).expanduser().resolve()
    if not p.exists():
        raise ValueError(f"{label} path does not exist: {raw}")
    if not p.is_dir():
        raise ValueError(f"{label} path is not a directory: {raw}")
    return p


@overload
def _coerce_dir_path(raw: str, label: str) -> str: ...


@overload
def _coerce_dir_path(raw: None, label: str) -> None: ...


def _coerce_dir_path(raw: str | None, label: str) -> str | None:
    path = _coerce_dir(raw, label)
    return str(path) if path is not None else None


def _coerce_skills_dirs(raw_dirs: list[str] | None) -> list[Path]:
    if not raw_dirs:
        return []
    result: list[Path] = []
    for raw in raw_dirs:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        p = p.resolve()
        if not p.exists():
            raise ValueError(f"--skills-dir path does not exist: {raw}")
        if not p.is_dir():
            raise ValueError(f"--skills-dir path is not a directory: {raw}")
        result.append(p)
    return result


def create_run_context(
    config: Config,
    exp_name: str,
    input_path: str,
    accuracy_command: str,
    benchmark_command: str,
    workspace_seed: Path | None = None,
    evaluator_path: Path | None = None,
    existing: bool = False,
    debug: bool = False,
    profiler_kind: ProfilerKind = ProfilerKind.AUTO,
    profiler_domain: DomainName = DomainName.LLM_SERVING,
    skills_dirs: list[str] | None = None,
    run_environment: RunEnvironmentSpec | None = None,
    git_tracking: bool = False,
    agent_backend: str | None = None,
    cli_provider: str | None = None,
    backend: ComputeBackend = DEFAULT_COMPUTE_BACKEND,
    environment_hooks: EnvironmentHooks | None = None,
) -> "_RunContext":
    """Build a fully wired :class:`_RunContext`.

    All construction side effects live here — run directory and log
    bootstrap, workspace materialization, backend/model construction,
    profiler resolution, git tracking init, run-environment session open,
    and agent-runner build.  ``_RunContext.__init__`` itself only assigns
    the assembled components.
    """
    config = as_config(config)
    run_environment_spec = run_environment or make_run_environment_spec()
    environment = build_run_environment(run_environment_spec)

    exp_dir = setup_exp_dir(exp_name, PROJECT_ROOT, existing)

    log_dir = exp_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    from vibesys.server.registry import active_supervisor

    supervisor = active_supervisor()
    if supervisor is not None:
        supervisor.attach(log_dir)
    # The stderr tee captures diagnostics into the run log; it never renders,
    # so it is safe under both the TUI and the headless renderer.
    logger = RunLogger(log_dir)

    # One teardown stack for the whole context: every component with
    # cleanup registers here, and close() unwinds it in reverse
    # construction order (device → environment hooks → session → logs).
    teardown_stack = ExitStack()
    teardown_stack.callback(logger.close)

    # Presentation is selected exactly once, here: with a TUI supervisor
    # attached, events flow to the supervision client; otherwise the
    # headless renderer subscribes to the same event stream and owns the
    # terminal for the lifetime of the run.
    if supervisor is None:
        renderer = HeadlessRenderer()
        teardown_stack.callback(output_sink().subscribe(renderer.handle))

    paths = RunPaths(
        exp_dir=exp_dir,
        log_dir=log_dir,
        workspace=exp_dir / "workspace",
        run_log_path=logger.path,
    )

    # Construct the platform backend (image + GPU spec come from it).
    backend_impl = backends.get(
        backend,
        log_dir=log_dir,
        log=logger.lprint,
        image=environment.backend_image,
    )
    # Resolve agent backend + cli provider early so Docker setup can
    # add provider-specific bind mounts and init commands.
    resolved_backend = agent_backend or config.agent.backend or DEFAULT_AGENT_BACKEND
    resolved_cli_provider = cli_provider or config.agent.cli_provider or "codex"

    model = build_model(config)
    model_name = config.model.name

    input_path_str = _coerce_dir_path(input_path, "--input")
    workspace_seed_path = _coerce_dir(workspace_seed, "workspace.seed")
    evaluator_source = _coerce_dir(evaluator_path, "evaluator.source")
    resolved_profiler_kind = resolve_profiler_kind(
        profiler_kind,
        domain=profiler_domain,
        backend_profiler_kind=getattr(backend_impl, "profiler_kind", None),
        environment_default_profiler_kind=environment.default_profiler_kind,
    )
    profiler_preflight = preflight_profiler_kind(resolved_profiler_kind)
    if not profiler_preflight.usable:
        raise ConfigurationError(
            ConfigurationDiagnostic(
                code="profiler_preflight_failed",
                stage="profiler_preflight",
                message=profiler_preflight.error_message(),
            )
        )

    profiler_support_path: str | None = None
    profiler_support_name: str | None = None
    if resolved_profiler_kind in ACTIVE_PROFILER_KINDS:
        definition = profiler_definition(resolved_profiler_kind)
        profiler_support_name = definition.support_name
        default_support = PROJECT_ROOT / "resources" / "profilers" / definition.kind.value
        if default_support.is_dir():
            profiler_support_path = str(default_support)

    skill_source_paths = _coerce_skills_dirs(skills_dirs)

    input_dir = Path(input_path_str)
    ref_dir: Path | None = input_dir / "reference"
    if ref_dir.exists():
        if not ref_dir.is_dir():
            raise ValueError(f"reference path is not a directory: {ref_dir}")
        reference_py = sorted(ref_dir.glob("*.py"))
        ref_name = f"reference/{reference_py[0].name}" if len(reference_py) == 1 else "reference"
    else:
        ref_dir = None
        ref_name = "."

    environment_reference = ref_dir or (input_dir / "reference")
    input_project_dir = input_dir if (input_dir / "pyproject.toml").is_file() else None

    workspace_files = Workspace(
        paths.workspace,
        run_environment=environment,
        backend=backend_impl,
        log=logger.lprint,
        project_root=PROJECT_ROOT,
    )
    workspace_files.create()

    # Fix ownership of workspace files that may have been created as root
    # by a previous Docker run, so the agent can write to them.
    if existing:
        workspace_files.repair()

    hooks = environment_hooks or NoopEnvironmentHooks()
    environment_context = EnvironmentContext(
        reference_path=environment_reference,
        workspace=workspace_files.root,
        run_environment=environment,
        project_root=PROJECT_ROOT,
        log=logger.lprint,
    )
    environment_patch = hooks.prepare(environment_context)

    # When resuming an existing run, the plan skips full workspace file
    # setup — the workspace already contains reference files, skills, etc.
    # from the previous run.  Only skills are refreshed and profiler
    # harnesses ensured; see Workspace.plan_setup.
    plan = workspace_files.plan_setup(
        existing=existing,
        seed=workspace_seed_path,
        input_dir=input_dir,
        evaluator_source=evaluator_source,
        skill_sources=skill_source_paths,
        input_project_dir=input_project_dir,
        profiler_support_path=profiler_support_path,
        profiler_support_name=profiler_support_name,
        extra_input_excludes=environment_patch.copy_excludes,
    )
    workspace_files.setup(plan, existing=existing)

    git = GitTracker(
        workspace_files.root,
        log=logger.lprint,
        excluded_dirs=workspace_files.excluded_dirs,
    )
    if git_tracking:
        git.init(existing)

    session = teardown_stack.enter_context(
        environment.open(
            RunEnvironmentRequest(
                log_dir=log_dir,
                workspace=workspace_files.root,
                ref_dir=ref_dir,
                backend=backend_impl,
                agent_backend=resolved_backend,
                cli_provider=resolved_cli_provider,
                accuracy_command=accuracy_command,
                benchmark_command=benchmark_command,
                profiler_support_path=profiler_support_path,
                profiler_support_name=profiler_support_name,
                environment_bind_mounts=environment_patch.bind_mounts,
                log=logger.lprint,
                project_root=PROJECT_ROOT,
            )
        )
    )
    # Snapshot the agent-facing commands once the session is open; the
    # view's paths are fixed for the session lifetime.
    commands = RunCommands(
        judge_accuracy_command=session.view.paths.accuracy_command,
        judge_benchmark_command=session.view.paths.benchmark_command,
        profiler_support_agent_path=session.view.paths.profiler_support,
        profiler_benchmark_command=session.view.paths.benchmark_command,
    )

    def _teardown_environment_hooks() -> None:
        try:
            hooks.teardown(environment_context)
        except Exception as exc:
            logger.lprint(f"[warn] environment hook teardown failed: {exc}")

    teardown_stack.callback(_teardown_environment_hooks)

    # Start backend-specific background monitoring (CUDA: nvidia-smi).
    device = DeviceLease(backend_impl, log_dir=log_dir, run_environment_view=session.view)
    device.start_monitor()
    teardown_stack.callback(device.close)

    # Build the backend-agnostic agent runner. Loops invoke this instead
    # of calling create_deep_agent / vibesys._agent_cli directly. The cli
    # backend is rejected if --docker is set; build_agent_runner raises
    # SystemExit with a clear message in that case.
    agent_runner = build_agent_runner(
        config,
        agent_backend=agent_backend,
        cli_provider=cli_provider,
        backends={
            "implementer": session.sandbox,
            "judge": session.sandbox,
            # Perf eval reuses the implementer's backend today (loop.py:564),
            # so the runner picks the same one when kind="perf_eval".
            "perf_eval": session.sandbox,
            # Profiler also reuses the implementer's backend — it needs
            # shell access to start/stop the server and run nsys.
            "profiler": session.sandbox,
            # Orchestrator (orchestrate loop) inspects the workspace
            # and writes plans — reuse the implementer's backend for
            # file access.
            "orchestrator": session.sandbox,
        },
        skills=[src.name for src in skill_source_paths],
        skill_source_dirs=skill_source_paths,
        model=model,
        model_name=model_name,
        run_log_file=logger.file,
        use_docker=(session.view.cli_sandboxed and not session.view.cli_modal_sandboxed),
        use_modal=session.view.cli_modal_sandboxed,
        log_dir=log_dir,
    )

    return _RunContext(
        backend=backend,
        run_environment=environment,
        supervisor=supervisor,
        logger=logger,
        paths=paths,
        debug=debug,
        git_tracking=git_tracking,
        backend_impl=backend_impl,
        model=model,
        model_name=model_name,
        input_path=input_path_str,
        workspace_seed_path=workspace_seed_path,
        evaluator_path=evaluator_source,
        accuracy_command=accuracy_command,
        benchmark_command=benchmark_command,
        profiler_kind=resolved_profiler_kind,
        profiler_support_path=profiler_support_path,
        profiler_support_name=profiler_support_name,
        skill_source_paths=skill_source_paths,
        ref_name=ref_name,
        environment_hooks=hooks,
        environment_context=environment_context,
        environment_patch=environment_patch,
        workspace_files=workspace_files,
        git=git,
        teardown_stack=teardown_stack,
        run_environment_session=session,
        commands=commands,
        device=device,
        agent_runner=agent_runner,
    )


class _RunContext:
    """Experiment lifecycle owner shared by simple, orchestrate, and issue loops.

    ``_RunContext`` sits above the run-environment abstraction:

        loop -> _RunContext -> RunEnvironment -> ComputeBackendImpl.make_sandbox -> Sandbox

    Instances are assembled by :func:`create_run_context`, which owns every
    construction side effect (run directory, log files, unified workspace,
    model, compute backend, copied helper inputs, git/snapshot tracking,
    run-environment session, agent runner, GPU monitor).  Environment-specific
    setup should stay in ``vibesys.sandbox.run_environment``; this class only
    asks the selected run environment for policy decisions and the opened
    sandbox session.
    """

    def __init__(
        self,
        *,
        backend: ComputeBackend,
        run_environment: RunEnvironment,
        supervisor: "RunSupervisor | None",
        logger: RunLogger,
        paths: RunPaths,
        debug: bool,
        git_tracking: bool,
        backend_impl: ComputeBackendImpl,
        model: Any,
        model_name: str,
        input_path: str | None,
        workspace_seed_path: Path | None,
        evaluator_path: Path | None,
        accuracy_command: str,
        benchmark_command: str,
        profiler_kind: ProfilerKind,
        profiler_support_path: str | None,
        profiler_support_name: str | None,
        skill_source_paths: list[Path],
        ref_name: str,
        environment_hooks: EnvironmentHooks,
        environment_context: EnvironmentContext,
        environment_patch: EnvironmentPatch,
        workspace_files: Workspace,
        git: GitTracker,
        teardown_stack: ExitStack,
        run_environment_session: RunEnvironmentSession,
        commands: RunCommands,
        device: DeviceLease,
        agent_runner: AgentRunner,
    ):
        self.backend = backend
        self.run_environment = run_environment
        self.supervisor = supervisor
        self.logger = logger
        self._paths = paths
        self.debug = debug
        self.git_tracking = git_tracking
        self.backend_impl = backend_impl
        self.model = model
        self.model_name = model_name
        self.input_path = input_path
        self.workspace_seed_path = workspace_seed_path
        self.evaluator_path = evaluator_path
        self.accuracy_command = accuracy_command
        self.benchmark_command = benchmark_command
        self.profiler_kind = profiler_kind
        self.profiler_support_path = profiler_support_path
        self.profiler_support_name = profiler_support_name
        self._skill_source_paths = skill_source_paths
        self.skills_for_agents = [src.name for src in skill_source_paths]
        self.ref_name = ref_name
        self.environment_hooks = environment_hooks
        self.environment_context = environment_context
        self.environment_patch = environment_patch
        self.workspace_files = workspace_files
        self.EXCLUDED_WORKSPACE_DIRS = workspace_files.excluded_dirs
        self.git = git
        self._teardown_stack = teardown_stack
        self.run_environment_session = run_environment_session
        self.run_environment_view = run_environment_session.view
        self.implementer_backend = run_environment_session.sandbox
        self.judge_backend = run_environment_session.sandbox
        self.commands = commands
        self.device = device
        # Expose the picked device for legacy callers (gpu monitor tests etc).
        self.selected_gpu = device.selected_device
        self.agent_runner = agent_runner
        self._closed = False
        self._progress_stack: list[AgentProgress] = []

    # -- path passthroughs ----------------------------------------------------
    # Canonical values live in the frozen ``RunPaths`` record; these
    # properties keep existing ``ctx.exp_dir``-style call sites working.

    @property
    def exp_dir(self) -> Path:
        return self._paths.exp_dir

    @property
    def log_dir(self) -> Path:
        return self._paths.log_dir

    @property
    def workspace(self) -> Path:
        return self._paths.workspace

    @property
    def run_log_path(self) -> Path:
        return self._paths.run_log_path

    @property
    def run_log_file(self) -> TextIO:
        """The current open log file handle (owned by ``RunLogger``)."""
        return self.logger.file

    @property
    def gpu_monitor(self) -> "ContentionMonitor | None":
        """The active device monitor (owned by ``DeviceLease``)."""
        return self.device.monitor

    @gpu_monitor.setter
    def gpu_monitor(self, monitor: "ContentionMonitor | None") -> None:
        self.device.monitor = monitor

    def gpu_env(self) -> dict[str, str]:
        """Env vars for the host-running cli agent runner — see :meth:`DeviceLease.gpu_env`."""
        return self.device.gpu_env()

    @contextmanager
    def progress(self, progress: AgentProgress) -> Generator[None]:
        """Temporarily attach loop progress to agent invocations in this context."""
        self._progress_stack.append(progress)
        try:
            yield
        finally:
            self._progress_stack.pop()

    def current_progress(self) -> AgentProgress | None:
        """Return the active loop progress, if a loop has scoped one."""
        if not self._progress_stack:
            return None
        return self._progress_stack[-1]

    def invoke(
        self,
        *,
        kind: str,
        system_prompt: str,
        user_prompt: str,
        response_cls: type[T],
        fallback_factory: Callable[[], T],
        round_label: str = "",
        progress: AgentProgress | None = None,
        **extra: Any,
    ) -> T:
        """Invoke an agent through ``self.agent_runner`` with workspace+env defaults.

        Wraps ``self.agent_runner.invoke(...)`` so the per-call boilerplate
        (``workspace=self.workspace``, ``env=self.gpu_env()``) doesn't have
        to be repeated at every call site.  Extra kwargs are forwarded to
        ``agent_runner.invoke`` unchanged so loop-specific options
        (e.g. ``iteration=`` for plain-loop runner extensions) still work.
        """
        supervisor = getattr(self, "supervisor", None)
        if supervisor is not None:
            supervisor.before_agent(kind, round_label, user_prompt, system_prompt)
        result: T | None = None
        error: BaseException | None = None
        try:
            result = self.agent_runner.invoke(
                kind=kind,
                workspace=self.workspace,
                system_prompt=system_prompt,
                env=self.gpu_env(),
                user_prompt=user_prompt,
                response_cls=response_cls,
                fallback_factory=fallback_factory,
                round_label=round_label,
                progress=progress if progress is not None else self.current_progress(),
                **extra,
            )
            return result
        except BaseException as exc:
            error = exc
            raise
        finally:
            if supervisor is not None:
                supervisor.after_agent(kind, round_label, result=result, error=error)

    def wait_for_debug(self, step: str) -> None:
        if self.debug:
            input(f"\n[debug] {step}. Press Enter to continue...")

    def snapshot_workspace(self, label: str) -> None:
        # Under --modal the implementer writes land in the ephemeral Modal
        # workspace Volume, not the host workspace dir. Pull the latest
        # state back to the host before snapshotting so git commits (or
        # directory copies) actually capture the implementer's code and
        # tests, not just ``progress.md``. The ModalSandbox's tar-and-
        # stream download is idempotent and excludes ``.venv`` /
        # ``__pycache__`` / mounted RO dirs — same set we already skip at
        # run end — so this is safe to run on every snapshot.
        if hasattr(self.implementer_backend, "_download_workspace"):
            try:
                self.implementer_backend._download_workspace()  # pyright: ignore[reportAttributeAccessIssue]
            except Exception as exc:
                self.lprint(f"[warn] modal workspace sync to host failed: {exc}")

        if self.git_tracking:
            self.git.snapshot(label)
            return
        dst = self.log_dir / "snapshots" / label
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(
            self.workspace,
            dst,
            symlinks=True,
            ignore=lambda _, names: [n for n in names if n in self.EXCLUDED_WORKSPACE_DIRS],
        )
        self.workspace_files.replace_external_symlinks(dst)

    def trusted_input_changes(self) -> list[str]:
        """Return evaluator-owned paths changed since workspace initialization."""
        if not self.git_tracking:
            return []
        return self.git.trusted_input_changes()

    # -- command passthroughs -------------------------------------------------
    # Canonical values live in the frozen ``RunCommands`` snapshot; these
    # properties keep existing ``ctx.judge_accuracy_command``-style call
    # sites working.

    @property
    def judge_accuracy_command(self) -> str | None:
        """Return the accuracy command as seen by the judge agent."""
        return self.commands.judge_accuracy_command

    @property
    def judge_benchmark_command(self) -> str | None:
        """Return the benchmark command as seen by the judge agent."""
        return self.commands.judge_benchmark_command

    @property
    def profiler_support_agent_path(self) -> str | None:
        """Return the selected profiler support path as seen by its agent."""
        return self.commands.profiler_support_agent_path

    @property
    def profiler_benchmark_command(self) -> str | None:
        """Return the benchmark command as seen by the profiler agent."""
        return self.commands.profiler_benchmark_command

    def lprint(self, text: str) -> None:
        self.logger.lprint(text)

    def switch_log_file(self, label: int | str) -> None:
        """Switch to a per-phase log file — see :meth:`RunLogger.switch`."""
        new_file = self.logger.switch(label)
        self._paths = replace(self._paths, run_log_path=self.logger.path)
        # Update the agent runner's log file handle so subsequent
        # invoke() calls write to the new step log.
        if hasattr(self, "agent_runner") and hasattr(self.agent_runner, "_run_log_file"):
            self.agent_runner._run_log_file = new_file  # pyright: ignore[reportAttributeAccessIssue]

    def reselect_gpu(self) -> None:
        """Delegate mid-run device rebalance — see :meth:`DeviceLease.reselect`."""
        self.device.reselect()
        # Mirror backend state on _RunContext for legacy callers/tests.
        self.selected_gpu = self.device.selected_device

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        # Unwinds in reverse construction order: device monitor stop +
        # gpu.json finalization, environment hook teardown, run-environment
        # session exit, stderr restore + log file close.
        self._teardown_stack.close()

    def __enter__(self) -> "_RunContext":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
