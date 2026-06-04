"""Shared run context: _RunContext, setup_exp_dir, _TeeWriter, _ensure_model_weights."""

import json
import os
import shutil
import subprocess
import sys
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

from vibe_serve import backends
from vibe_serve.agent_runner import _log_and_print
from vibe_serve.agents import build_agent_runner
from vibe_serve.config import Config, as_config
from vibe_serve.constants import (
    ComputeBackend,
    DEFAULT_AGENT_BACKEND,
    DEFAULT_COMPUTE_BACKEND,
    PROJECT_ROOT,
)
from vibe_serve.llm_client import _build_model
from vibe_serve.sandbox.run_environment import (
    RunEnvironmentRequest,
    RunEnvironmentSpec,
    build_run_environment,
    make_run_environment_spec,
)


def setup_exp_dir(
    exp_name: str,
    project_root: Path = PROJECT_ROOT,
    existing: bool = False,
) -> Path:
    """Create or validate exp_env/<timestamp>-<exp_name>/ directory with git init."""
    if existing:
        exp_dir = project_root / "exp_env" / exp_name
    else:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        exp_dir = project_root / "exp_env" / f"{timestamp}-{exp_name}"
    if existing:
        if not exp_dir.is_dir():
            raise FileNotFoundError(f"Experiment directory not found: {exp_dir}")
        return exp_dir
    exp_dir.mkdir(parents=True, exist_ok=True)
    if not (exp_dir / ".git").is_dir():
        subprocess.run(
            ["git", "init"],
            cwd=exp_dir,
            capture_output=True,
            check=True,
        )
    return exp_dir


def _ensure_model_weights(ref_dir: Path) -> None:
    """Ensure model weights exist in ref_dir/model, downloading if needed."""
    model_path = ref_dir / "model"

    # Remove broken symlink if present
    if model_path.is_symlink() and not model_path.exists():
        model_path.unlink()

    # Already exists (real dir or valid symlink)
    if model_path.exists():
        return

    # Read meta.json for download info
    meta_path = ref_dir / "meta.json"
    if not meta_path.exists():
        raise FileNotFoundError(
            f"Model weights not found at {model_path} and no meta.json to download from. "
            f"Either create a model/ directory/symlink or add a meta.json with model_id."
        )

    meta = json.loads(meta_path.read_text())
    model_id = meta.get("model_id")
    if not model_id:
        raise ValueError(f"meta.json at {meta_path} missing required 'model_id' field")

    revision = meta.get("revision")

    cache_dir = PROJECT_ROOT / ".hf_cache"
    print(f"[model] Weights not found at {model_path}. Downloading {model_id} to {cache_dir}...")
    from huggingface_hub import snapshot_download

    downloaded_path = snapshot_download(model_id, revision=revision, cache_dir=str(cache_dir))

    model_path.symlink_to(downloaded_path)
    print(f"[model] Created symlink {model_path} -> {downloaded_path}")


class _TeeWriter:
    def __init__(self, primary, secondary):
        self._primary = primary
        self._secondary = secondary

    def write(self, text):
        self._primary.write(text)
        self._secondary.write(text)
        return len(text)

    def flush(self):
        self._primary.flush()
        self._secondary.flush()

    def isatty(self):
        return False


class _RunContext:
    """Experiment lifecycle owner shared by simple, orchestrate, and issue loops.

    ``_RunContext`` sits above the run-environment abstraction:

        loop -> _RunContext -> RunEnvironment -> ComputeBackendImpl.make_sandbox -> Sandbox

    It creates the run directory, log files, unified workspace, model, compute
    backend, copied helper inputs, git/snapshot tracking, run-environment
    session, agent runner, and GPU monitor. Environment-specific setup should stay in
    ``vibe_serve.sandbox.run_environment``; this class should only ask the selected
    run environment for policy decisions and the opened sandbox session.
    """

    def __init__(
        self,
        config: Config,
        exp_name: str,
        reference_path: str,
        existing: bool = False,
        debug: bool = False,
        acc_checker: str | None = None,
        bench: str | None = None,
        nsys_profiler: str | None = None,
        torch_profiler: str | None = None,
        profiler_kind: str = "auto",
        skills_dirs: list[str] | None = None,
        run_environment: RunEnvironmentSpec | None = None,
        git_tracking: bool = False,
        agent_backend: str | None = None,
        cli_provider: str | None = None,
        backend: ComputeBackend = DEFAULT_COMPUTE_BACKEND,
    ):
        config = as_config(config)
        self.backend: ComputeBackend = backend
        run_environment = run_environment or make_run_environment_spec()
        self.run_environment = build_run_environment(run_environment)

        self.exp_dir = setup_exp_dir(exp_name, PROJECT_ROOT, existing)

        self.log_dir = self.exp_dir / "logs"
        self.log_dir.mkdir(parents=True, exist_ok=True)
        run_started = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_log_path = self.log_dir / f"run-{run_started}.log"
        self.run_log_file = self.run_log_path.open("a", encoding="utf-8")

        self._original_stderr = sys.stderr
        sys.stderr = _TeeWriter(self._original_stderr, self.run_log_file)
        self._closed = False
        self._run_environment_stack = ExitStack()

        # Dirs excluded from workspace copy, git tracking, and the
        # Modal-side tar download. ``_auth`` and ``_opt_vibeserve`` are
        # our own "bind-mount redirect" dirs under --modal (host auth +
        # vibe_serve pkg uploaded into /workspace/_auth and
        # /workspace/_opt_vibeserve respectively) — not implementer
        # output, and we never want them in git history. ``_mounts`` is
        # the Docker ancestor-mount redirect dir for the same reason.
        # ``.cache`` holds any HF-download fallback (drafter, etc.).
        self.EXCLUDED_WORKSPACE_DIRS = {
            ".claude", "__pycache__", ".git", "repos",
            "_auth", "_opt_vibeserve", "_mounts", ".cache",
        }
        self.debug = debug
        self.git_tracking = git_tracking

        # Construct the platform backend (image + GPU spec come from it).
        self.backend_impl = backends.get(
            self.backend,
            log_dir=self.log_dir,
            log=lambda msg: self.lprint(msg),
            image=self.run_environment.backend_image,
        )
        # Resolve agent backend + cli provider early so Docker setup can
        # add provider-specific bind mounts and init commands.
        self._resolved_backend = (
            agent_backend or config.agent.backend or DEFAULT_AGENT_BACKEND
        )
        self._cli_provider = cli_provider or config.agent.cli_provider or "codex"

        self.model = _build_model(config)
        self.model_name = config.model.name

        self.acc_checker_path = self._coerce_dir_path(acc_checker, "--acc-checker")
        self.bench_path = self._coerce_dir_path(bench, "--bench")
        self.nsys_profiler_path = self._coerce_dir_path(nsys_profiler, "--nsys-profiler")
        self.torch_profiler_path = self._coerce_dir_path(torch_profiler, "--torch-profiler")

        # Resolve profiler kind: 'auto' → the run environment's default.
        resolved_profiler = profiler_kind
        if resolved_profiler == "auto":
            resolved_profiler = self.run_environment.default_profiler_kind
        if resolved_profiler not in ("nsys", "torch"):
            raise ValueError(f"Unknown profiler kind: {profiler_kind!r}")
        self.profiler_kind = resolved_profiler

        # Default torch_profiler_path to examples/torch_profiler/ if --profiler=torch
        # and the user didn't explicitly set --torch-profiler.
        if self.profiler_kind == "torch" and self.torch_profiler_path is None:
            default_tp = PROJECT_ROOT / "examples" / "torch_profiler"
            if default_tp.is_dir():
                self.torch_profiler_path = str(default_tp)

        skill_source_paths = self._coerce_skills_dirs(skills_dirs)
        self._skill_source_paths: list[Path] = skill_source_paths

        ref_path = Path(reference_path).expanduser().resolve()
        if not ref_path.exists():
            raise ValueError(f"Reference path does not exist: {reference_path}")

        ref_dir: Path | None = None
        if ref_path.is_file():
            ref_script = ref_path
            self.ref_name = ref_script.name
        elif ref_path.is_dir():
            reference_py = sorted(ref_path.glob("*.py"))
            if not reference_py:
                raise ValueError(f"No reference Python script found in directory: {reference_path}")
            if len(reference_py) != 1:
                raise ValueError(
                    f"Expected one reference Python script in {reference_path}, found {len(reference_py)}"
                )
            ref_script = reference_py[0]
            ref_dir = ref_path
            self.ref_name = f"reference/{ref_script.name}"
        else:
            raise ValueError(f"Reference path is invalid: {reference_path}")

        self.workspace = self.exp_dir / "workspace"
        self.workspace.mkdir(parents=True, exist_ok=True)

        # When resuming an existing run, skip workspace file setup — the
        # workspaces already contain reference files, skills, etc. from the
        # previous run.  Only re-initialize backends and logging.
        self.skills_for_agents: list[str] = [src.name for src in skill_source_paths]

        # Fix ownership of workspace files that may have been created as root
        # by a previous Docker run, so the agent can write to them.
        if existing:
            self.run_environment.repair_workspace(
                self.workspace,
                backend=self.backend_impl,
                log=self.lprint,
            )

        # Always refresh skills into the workspace (even on --resume). Skill
        # source is tiny (MB) and copying is cheap; without this, an
        # interrupted run leaves stale skills from the previous CLI version
        # in the host workspace, which Modal then uploads verbatim into the
        # fresh sandbox volume at start, and codex-cli fails to load them
        # (e.g. skill description exceeds a newer CLI's length limit).
        # Mirrors _materialize_skills destinations inside cli_runner.
        _cli_skill_dirs = (".agents/skills", ".claude/skills", ".gemini/skills", ".cursor/skills", ".opencode/skills")
        for src in skill_source_paths:
            rel = src.name
            if (self.workspace / rel).exists():
                self._copy_excluding_extras(src, self.workspace / rel)
            for cli_rel in _cli_skill_dirs:
                cli_target = self.workspace / cli_rel / rel
                if cli_target.exists():
                    self._copy_excluding_extras(src, cli_target)

        if not existing:
            for excluded in self.EXCLUDED_WORKSPACE_DIRS:
                d = self.workspace / excluded
                if d.exists():
                    shutil.rmtree(d)

            if ref_dir is not None:
                # Modal handles model weights via a remote Volume, so we
                # skip the local HF download (saves ~30 GB of local cache).
                # We still fall back to the local path if meta.json is absent.
                if (
                    self.run_environment.materialize_local_model_weights
                    or (ref_dir / "meta.json").exists() is False
                ):
                    _ensure_model_weights(ref_dir)
                self._copy_excluding_extras(ref_dir, self.workspace / "reference")
            else:
                if (self.workspace / ref_script.name).exists():
                    (self.workspace / ref_script.name).unlink()
                shutil.copy2(ref_script, self.workspace / ref_script.name)

            for src in skill_source_paths:
                rel = src.name
                self._copy_excluding_extras(src, self.workspace / rel)

            # Copy acc_checker and bench into the workspace so agents can
            # access them directly (not only via Docker bind mounts).
            if self.acc_checker_path:
                src = Path(self.acc_checker_path)
                self._copy_excluding_extras(src, self.workspace / "acc_checker")
            if self.bench_path:
                src = Path(self.bench_path)
                self._copy_excluding_extras(src, self.workspace / "bench")

            if self.nsys_profiler_path:
                src = Path(self.nsys_profiler_path)
                self._copy_excluding_extras(src, self.workspace / "nsys_profiler")
            if self.torch_profiler_path:
                src = Path(self.torch_profiler_path)
                self._copy_excluding_extras(src, self.workspace / "torch_profiler")

        # Always ensure profiler harnesses are present in the workspace, even
        # when resuming — the original run may not have had them.
        if existing and self.nsys_profiler_path:
            src = Path(self.nsys_profiler_path)
            if not (self.workspace / "nsys_profiler").exists():
                self._copy_excluding_extras(src, self.workspace / "nsys_profiler")
        if existing and self.torch_profiler_path:
            src = Path(self.torch_profiler_path)
            if not (self.workspace / "torch_profiler").exists():
                self._copy_excluding_extras(src, self.workspace / "torch_profiler")

        if git_tracking:
            self._init_git_tracking(existing)

        self.run_environment_session = self._run_environment_stack.enter_context(
            self.run_environment.open(
                RunEnvironmentRequest(
                    log_dir=self.log_dir,
                    workspace=self.workspace,
                    ref_dir=ref_dir,
                    backend=self.backend_impl,
                    agent_backend=self._resolved_backend,
                    cli_provider=self._cli_provider,
                    acc_checker_path=self.acc_checker_path,
                    bench_path=self.bench_path,
                    nsys_profiler_path=self.nsys_profiler_path,
                    torch_profiler_path=self.torch_profiler_path,
                    log=self.lprint,
                    project_root=PROJECT_ROOT,
                )
            )
        )
        self.run_environment_view = self.run_environment_session.view
        self.implementer_backend = self.run_environment_session.sandbox
        self.judge_backend = self.run_environment_session.sandbox

        # Expose the picked device for legacy callers (gpu monitor tests etc).
        self.selected_gpu = getattr(self.backend_impl, "selected_device", None)

        # Start backend-specific background monitoring (CUDA: nvidia-smi).
        self.gpu_monitor = self.backend_impl.make_monitor(self.log_dir)
        if self.gpu_monitor is not None:
            self.gpu_monitor.start()

        # Build the backend-agnostic agent runner. Loops invoke this instead
        # of calling create_deep_agent / vibe_serve._agent_cli directly. The cli
        # backend is rejected if --docker is set; build_agent_runner raises
        # SystemExit with a clear message in that case.
        self.agent_runner = build_agent_runner(
            config,
            agent_backend=agent_backend,
            cli_provider=cli_provider,
            backends={
                "implementer": self.implementer_backend,
                "judge": self.judge_backend,
                # Perf eval reuses the implementer's backend today (loop.py:564),
                # so the runner picks the same one when kind="perf_eval".
                "perf_eval": self.implementer_backend,
                # Profiler also reuses the implementer's backend — it needs
                # shell access to start/stop the server and run nsys.
                "profiler": self.implementer_backend,
                # Orchestrator (orchestrate loop) inspects the workspace
                # and writes plans — reuse the implementer's backend for
                # file access.
                "orchestrator": self.implementer_backend,
            },
            skills=self.skills_for_agents,
            skill_source_dirs=self._skill_source_paths,
            model=self.model,
            model_name=self.model_name,
            run_log_file=self.run_log_file,
            use_docker=(
                self.run_environment_view.cli_sandboxed
                and not self.run_environment_view.cli_modal_sandboxed
            ),
            use_modal=self.run_environment_view.cli_modal_sandboxed,
            log_dir=self.log_dir,
        )

    def gpu_env(self) -> dict[str, str]:
        """Env vars to inject into the host-running cli agent runner.

        Today this is just the device pin (``CUDA_VISIBLE_DEVICES`` for cuda),
        derived from whichever device the backend selected.  The deepagents
        path ignores this; the cli path layers it onto the spawned subprocess
        env so it sees the same device the sandbox env was built with.
        """
        dev = getattr(self.backend_impl, "selected_device", None)
        if dev is None:
            return {}
        return {"CUDA_VISIBLE_DEVICES": str(dev.index)}

    def invoke(
        self,
        *,
        kind: str,
        system_prompt: str,
        user_prompt: str,
        response_cls,
        fallback_factory=None,
        round_label: str = "",
        **extra,
    ):
        """Invoke an agent through ``self.agent_runner`` with workspace+env defaults.

        Wraps ``self.agent_runner.invoke(...)`` so the per-call boilerplate
        (``workspace=self.workspace``, ``env=self.gpu_env()``) doesn't have
        to be repeated at every call site.  Extra kwargs are forwarded to
        ``agent_runner.invoke`` unchanged so loop-specific options
        (e.g. ``iteration=`` for plain-loop runner extensions) still work.
        """
        return self.agent_runner.invoke(
            kind=kind,
            workspace=self.workspace,
            system_prompt=system_prompt,
            env=self.gpu_env(),
            user_prompt=user_prompt,
            response_cls=response_cls,
            fallback_factory=fallback_factory,
            round_label=round_label,
            **extra,
        )

    def _coerce_dir_path(self, raw: str | None, label: str) -> str | None:
        if raw is None:
            return None
        p = Path(raw).expanduser().resolve()
        if not p.exists():
            raise ValueError(f"{label} path does not exist: {raw}")
        if not p.is_dir():
            raise ValueError(f"{label} path is not a directory: {raw}")
        return str(p)

    def _coerce_skills_dirs(self, raw_dirs: list[str] | None) -> list[Path]:
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

    @staticmethod
    def _remove_external_symlinks(root: Path) -> None:
        """Remove symlinks pointing outside *root* (Docker bind mounts replace them)."""
        resolved_root = root.resolve()
        for path in list(root.rglob("*")):
            if path.is_symlink():
                target = path.resolve()
                try:
                    target.relative_to(resolved_root)
                except ValueError:
                    path.unlink()

    @staticmethod
    def _replace_external_symlinks(root: Path) -> None:
        """Replace symlinks pointing outside *root* with `<name>.symlink_target` files."""
        resolved_root = root.resolve()
        for path in list(root.rglob("*")):
            if path.is_symlink():
                target = path.resolve()
                try:
                    target.relative_to(resolved_root)
                except ValueError:
                    # Symlink points outside root — replace it
                    marker = path.parent / f"{path.name}.symlink_target"
                    path.unlink()
                    marker.write_text(str(target))

    def _copy_excluding_extras(self, src: Path, dst: Path) -> None:
        skip = self.EXCLUDED_WORKSPACE_DIRS | {"_mounts"}

        def _ignore(_: str, names: list[str]) -> list[str]:
            return [name for name in names if name in skip]

        if dst.exists():
            # Remove children individually so we can skip mount points and
            # tolerate permission errors (e.g. root-owned dirs left by Docker).
            for child in list(dst.iterdir()):
                if child.name in skip:
                    continue
                try:
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
                except PermissionError:
                    if not self.run_environment.remove_workspace_child(
                        dst,
                        child.name,
                        backend=self.backend_impl,
                    ):
                        self.lprint(
                            f"[warn] _copy_excluding_extras: could not "
                            f"remove {child.name} from {dst}"
                        )
        dst.mkdir(parents=True, exist_ok=True)
        for child in src.iterdir():
            if child.name in skip:
                continue
            child_dst = dst / child.name
            if child_dst.exists() or child_dst.is_symlink():
                # Stale leftover — try once more to remove before copying
                try:
                    if child_dst.is_dir() and not child_dst.is_symlink():
                        shutil.rmtree(child_dst)
                    else:
                        child_dst.unlink()
                except PermissionError:
                    self.lprint(
                        f"[warn] _copy_excluding_extras: {child.name} in "
                        f"{dst} is stale and could not be replaced"
                    )
                    continue
            try:
                if child.is_symlink():
                    os.symlink(os.readlink(child), child_dst)
                elif child.is_dir():
                    shutil.copytree(child, child_dst, symlinks=True, ignore=_ignore)
                else:
                    shutil.copy2(child, child_dst)
            except PermissionError:
                self.lprint(
                    f"[warn] _copy_excluding_extras: could not copy "
                    f"{child.name} to {dst}"
                )
        if self.run_environment.isolated:
            # In containerized mode, external symlinks become bind mounts
            # (Docker) or volume uploads (Modal). Remove the broken symlinks
            # so the mount point / volume path can host the resolved contents.
            self._remove_external_symlinks(dst)
        else:
            self._replace_external_symlinks(dst)

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
                self.implementer_backend._download_workspace()
            except Exception as exc:
                self.lprint(f"[warn] modal workspace sync to host failed: {exc}")

        if self.git_tracking:
            self._git_snapshot(label)
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
        self._replace_external_symlinks(dst)

    # -- git tracking helpers -------------------------------------------------

    _GIT_ENV_STATIC = {
        "GIT_AUTHOR_NAME": "vibeserve",
        "GIT_AUTHOR_EMAIL": "vibeserve@local",
        "GIT_COMMITTER_NAME": "vibeserve",
        "GIT_COMMITTER_EMAIL": "vibeserve@local",
    }

    @property
    def _GIT_ENV(self) -> dict[str, str]:
        """Git env with safe.directory set to workspace to avoid ownership errors."""
        return {
            **self._GIT_ENV_STATIC,
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "safe.directory",
            "GIT_CONFIG_VALUE_0": str(self.workspace),
        }

    def _git_run(self, cmd: list[str], *, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
        """Run a git command in workspace, logging stderr on failure."""
        if env is None:
            env = {**os.environ, **self._GIT_ENV}
        result = subprocess.run(cmd, cwd=self.workspace, capture_output=True, env=env)
        if check and result.returncode != 0:
            stderr = result.stderr.decode(errors="replace").strip()
            self.lprint(f"[git-tracking] command failed: {' '.join(cmd)}")
            self.lprint(f"[git-tracking] exit code {result.returncode}: {stderr}")
            result.check_returncode()
        return result

    def _init_git_tracking(self, existing: bool) -> None:
        """Initialize or validate the git repo in the unified workspace."""
        if existing:
            if not (self.workspace / ".git").is_dir():
                raise ValueError(
                    f"--git-tracking with --resume but no git repository in {self.workspace}"
                )
            return

        self._git_run(["git", "init"])

        gitignore = self.workspace / ".gitignore"
        gitignore.write_text("\n".join(sorted(self.EXCLUDED_WORKSPACE_DIRS)) + "\n")

        self._git_run(["git", "add", "-A"])
        self._git_run(["git", "commit", "-m", "initial: workspace setup"])

    def _git_snapshot(self, label: str) -> None:
        """Commit current workspace state with *label* as the commit message."""
        self._git_run(["git", "add", "-A"])
        # git diff --cached --quiet exits 1 when there are staged changes
        has_changes = self._git_run(
            ["git", "diff", "--cached", "--quiet"], check=False,
        ).returncode != 0
        if has_changes:
            self._git_run(["git", "commit", "-m", label])
        else:
            self.lprint(f"[git-tracking] no changes to commit for '{label}'")

    @property
    def judge_acc_checker_path(self) -> str | None:
        """Return the acc_checker path as seen by the judge agent."""
        return self.run_environment_view.paths.acc_checker

    @property
    def judge_bench_path(self) -> str | None:
        """Return the bench path as seen by the judge agent."""
        return self.run_environment_view.paths.bench

    @property
    def profiler_nsys_profiler_path(self) -> str | None:
        """Return the nsys_profiler path as seen by the profiler agent."""
        return self.run_environment_view.paths.nsys_profiler

    @property
    def profiler_torch_profiler_path(self) -> str | None:
        """Return the torch_profiler path as seen by the profiler agent."""
        return self.run_environment_view.paths.torch_profiler

    @property
    def profiler_bench_path(self) -> str | None:
        """Return the bench path as seen by the profiler agent."""
        return self.run_environment_view.paths.bench

    def lprint(self, text: str) -> None:
        _log_and_print(text, self.run_log_file)

    def switch_log_file(self, label: int | str) -> None:
        """Switch to a per-phase log file (``run-<datetime>-<label>.log``).

        *label* is stringified into the file name.  Integer labels get a
        ``step`` prefix for backward compatibility with the curriculum
        loop's step-number usage (e.g. ``switch_log_file(3)`` →
        ``run-<ts>-step3.log``).  Callers that want a different prefix
        (e.g. ``round007``) should pass a string.

        The previous log file is flushed but kept open (the ``_TeeWriter``
        on stderr still references it).  A new file is opened and both
        ``run_log_file`` and the stderr tee are updated.
        """
        self.run_log_file.flush()
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        suffix = f"step{label}" if isinstance(label, int) else label
        new_path = self.log_dir / f"run-{ts}-{suffix}.log"
        new_file = new_path.open("a", encoding="utf-8")
        self.run_log_path = new_path
        self.run_log_file = new_file
        sys.stderr = _TeeWriter(self._original_stderr, new_file)
        # Update the agent runner's log file handle so subsequent
        # invoke() calls write to the new step log.
        if hasattr(self, "agent_runner") and hasattr(self.agent_runner, "_run_log_file"):
            self.agent_runner._run_log_file = new_file

    def reselect_gpu(self) -> None:
        """Delegate mid-run device rebalance to the backend.

        Restarted sandboxes re-run their ``setup_fns`` (e.g. docker symlinks)
        as part of ``start()`` — no replay logic needed here.
        """
        run_environment_view = getattr(self, "run_environment_view", None)
        if run_environment_view is not None and not run_environment_view.host_device_reselect:
            return
        self.backend_impl.reselect_device()
        # Mirror backend state on _RunContext for legacy callers/tests.
        self.selected_gpu = getattr(self.backend_impl, "selected_device", None)
        self.gpu_monitor = getattr(self.backend_impl, "_monitor", None)

    def _finalize_gpu_metadata(self) -> None:
        """Update ``gpu.json`` with contention summary before closing."""
        gpu_json = self.log_dir / "gpu.json"
        if not gpu_json.exists():
            return

        data = json.loads(gpu_json.read_text())

        contention_log = self.log_dir / "gpu_contention.jsonl"
        contention_events = 0
        if contention_log.exists():
            text = contention_log.read_text().strip()
            if text:
                contention_events = len(text.splitlines())

        data["contention_detected"] = contention_events > 0
        data["contention_events"] = contention_events
        data["finished_at"] = datetime.now().isoformat()
        gpu_json.write_text(json.dumps(data, indent=2))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.gpu_monitor is not None:
            self.gpu_monitor.stop()
        self._finalize_gpu_metadata()
        self._run_environment_stack.close()
        sys.stderr = self._original_stderr
        self.run_log_file.close()

    def __enter__(self) -> "_RunContext":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
