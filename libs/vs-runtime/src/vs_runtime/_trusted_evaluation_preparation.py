"""Prepare trusted evaluator commands for one concrete execution environment."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from vs_runtime._evaluator_packages import load_evaluator_package
from vs_sandbox.api import BeforeReadyContext, SandboxLifecycleHooks
from vs_sandbox.api.command_translation import (
    PROJECT_ROOT_TOKEN,
    PYTHON_TOKEN,
    translate_command_arguments,
)
from vs_sandbox.api.evaluator_tools import (
    CargoGitToolSpec,
    evaluator_tools_install_command,
    tool_install_root,
    tool_path_replacements,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vs_runtime._trusted_evaluation import TrustedEvaluationPlan

type EvaluatorToolchain = Literal["go", "rust"]

SANDBOX_EVALUATOR_TOOLS_ROOT = Path("/opt/vibesys-evaluator-tools")
SANDBOX_EVALUATOR_GO_MODULES_ROOT = Path("/opt/vibesys-evaluator-go-modules")
REMOTE_EVALUATOR_TOOLS_ROOT = Path(".vibesys-evaluator-tools")
REMOTE_EVALUATOR_GO_MODULES_ROOT = Path(".vibesys-evaluator-go-modules")
REMOTE_EVALUATOR_TOOLCHAINS_ROOT = Path(".vibesys-evaluator-toolchains")

_EVALUATOR_RUST_TOOLCHAIN_VERSION = "1.92.0"
_DOCKER_EVALUATOR_CACHE_SCHEMA = 2
_GO_MODULE_SETUP_TIMEOUT_SECONDS = 660
_PACKAGE_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")
_PYTHON_DOWNLOAD_SCRIPT = """\
import sys
import time
import urllib.request

url = sys.argv[1].format(arch=sys.argv[3])
for attempt in range(5):
    try:
        urllib.request.urlretrieve(url, sys.argv[2])
        break
    except Exception:
        if attempt == 4:
            raise
        time.sleep(5)
"""
_GO_MODULE_VERIFY_SCRIPT = """\
import fcntl
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def remove_tree(path):
    def make_writable_and_retry(function, name, _error):
        os.chmod(name, stat.S_IRWXU)
        function(name)

    shutil.rmtree(path, onerror=make_writable_and_retry)


def lock_available(stream):
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


module_root, module_cache = sys.argv[1:]
scratch = Path(tempfile.gettempdir()) / f"vibesys-evaluator-go-verify-{os.getuid()}"
scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
scratch_info = os.lstat(scratch)
if not stat.S_ISDIR(scratch_info.st_mode) or scratch_info.st_uid != os.getuid():
    print(f"unsafe evaluator Go verification scratch: {scratch}", file=sys.stderr)
    raise SystemExit(1)
os.chmod(scratch, 0o700)

with (scratch / ".recovery.lock").open("a+b") as recovery:
    fcntl.flock(recovery.fileno(), fcntl.LOCK_EX)
    for stale in scratch.glob("cache.*"):
        if not stale.is_dir():
            continue
        stale_lease_path = scratch / f".lease.{stale.name}"
        try:
            stale_lease = stale_lease_path.open("a+b")
        except FileNotFoundError:
            continue
        with stale_lease:
            if not lock_available(stale_lease):
                continue
            try:
                remove_tree(stale)
            except FileNotFoundError:
                pass
            except OSError as error:
                print(
                    f"cannot recover stale evaluator Go verification cache {stale}: {error}",
                    file=sys.stderr,
                )
                raise SystemExit(1) from error
        stale_lease_path.unlink(missing_ok=True)
    for orphaned_lease in scratch.glob(".lease.cache.*"):
        work_name = orphaned_lease.name.removeprefix(".lease.")
        if (scratch / work_name).exists():
            continue
        try:
            orphaned = orphaned_lease.open("a+b")
        except FileNotFoundError:
            continue
        with orphaned:
            if lock_available(orphaned):
                orphaned_lease.unlink(missing_ok=True)
    work = Path(tempfile.mkdtemp(prefix="cache.", dir=scratch))
    lease_path = scratch / f".lease.{work.name}"
    lease = lease_path.open("a+b")
    fcntl.flock(lease.fileno(), fcntl.LOCK_EX)

interrupted = None
process = None
unwinding = False


def begin_interrupt():
    global unwinding
    if unwinding:
        return
    unwinding = True
    # Make cleanup one-shot before control leaves the interrupted operation.
    # A second handled signal must never raise from inside the finally block.
    signal.pthread_sigmask(signal.SIG_BLOCK, handled_signals)
    for handled in handled_signals:
        signal.signal(handled, signal.SIG_IGN)
    raise InterruptedError


def stop(signum, _frame):
    global interrupted
    if interrupted is None:
        interrupted = signum
    # Popen can dispatch a signal after fork but before publishing the child
    # handle. Record that signal without unwinding until the child is owned;
    # afterwards unwinding is safe because the finally block can stop it.
    if process is not None:
        begin_interrupt()


handled_signals = (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)
for handled in handled_signals:
    signal.signal(handled, stop)


def wait_without_reaping(process, timeout=None):
    # Observe leader exit while retaining its PID and process-group ownership.
    # Poll instead of blocking in waitid: a signal that lands after Python's
    # last signal check but before the syscall starts never interrupts it, so
    # a blocking waitid would sleep until the child exits and miss the interrupt.
    # Each pass re-enters the interpreter, so a pending handler runs within one tick.
    options = os.WEXITED | os.WNOWAIT | os.WNOHANG
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        result = os.waitid(os.P_PID, process.pid, options)
        if result is not None:
            return result
        remaining = None if deadline is None else deadline - time.monotonic()
        if remaining is not None and remaining <= 0:
            return None
        time.sleep(0.05 if remaining is None else min(0.05, remaining))

environment = os.environ.copy()
environment.update(
    GOCACHE=str(work),
    GOMODCACHE=module_cache,
    GOENV="off",
    GOFLAGS="-mod=readonly -buildvcs=false",
    GOPROXY="off",
    GOSUMDB="off",
    GOTOOLCHAIN="local",
    GOWORK="off",
)
status = 1
cleanup_error = None
try:
    process = subprocess.Popen(
        ["go", "-C", module_root, "list", "-deps", "./..."],
        env=environment,
        start_new_session=True,
        stdout=subprocess.DEVNULL,
    )
    if interrupted is not None:
        begin_interrupt()
    wait_without_reaping(process)
    # From here onward every path is cleanup. A first late signal may record
    # interruption, but must not raise from inside the finally block.
    unwinding = True
except InterruptedError:
    status = 128 + (interrupted or signal.SIGTERM)
finally:
    signal.pthread_sigmask(signal.SIG_BLOCK, handled_signals)
    for handled in handled_signals:
        signal.signal(handled, signal.SIG_IGN)
    if process is not None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        wait_without_reaping(process, timeout=5)
        # The group leader can exit before descendants have honored TERM.
        # Keep that leader unreaped so its numeric PGID cannot be reused, then
        # always retire the isolated group before deleting its shared cache.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        leader_status = process.wait()
        status = (
            leader_status
            if interrupted is None
            else 128 + (interrupted or signal.SIGTERM)
        )
    try:
        with (scratch / ".recovery.lock").open("a+b") as recovery:
            fcntl.flock(recovery.fileno(), fcntl.LOCK_EX)
            try:
                remove_tree(work)
            except FileNotFoundError:
                pass
            except OSError as error:
                cleanup_error = error
            if cleanup_error is None:
                lease_path.unlink(missing_ok=True)
    except OSError as error:
        if cleanup_error is None:
            cleanup_error = error
    try:
        lease.close()
    except OSError as error:
        if cleanup_error is None:
            cleanup_error = error

if cleanup_error is not None:
    print(
        f"cannot clean evaluator Go verification cache {work}: {cleanup_error}",
        file=sys.stderr,
    )
    raise SystemExit(1)
raise SystemExit(status)
"""


class EvaluatorGoModuleError(RuntimeError):
    """A declared evaluator Go module could not be prepared for offline use."""

    @classmethod
    def builder_incomplete(cls) -> EvaluatorGoModuleError:
        """Describe a target builder that returned without publishing its cache."""
        return cls("target-image builder did not publish the evaluator Go module cache")

    @classmethod
    def toolchain_setup_failed(cls, detail: str) -> EvaluatorGoModuleError:
        """Describe target-image Go installation failure."""
        return cls(f"target-image Go toolchain setup failed: {detail[-500:]}")

    @classmethod
    def module_setup_failed(cls, detail: str) -> EvaluatorGoModuleError:
        """Describe target-image dependency preparation failure."""
        return cls(f"target-image Go module setup failed: {detail[-500:]}")

    @classmethod
    def ownership_failed(cls, detail: str) -> EvaluatorGoModuleError:
        """Describe failure to return a built cache to its host owner."""
        return cls(f"could not return Go module cache ownership: {detail[-500:]}")


@dataclass(frozen=True, slots=True)
class EvaluatorGoModule:
    """One immutable package module and its machine-local dependency cache."""

    root: Path
    cache_root: Path
    package_digest: str

    def __post_init__(self) -> None:
        """Reject identities that cannot safely name an atomic readiness marker."""
        if _PACKAGE_DIGEST_PATTERN.fullmatch(self.package_digest) is None:
            message = "evaluator Go module package digest must be a sha256 identity"
            raise ValueError(message)


def evaluator_go_module_ready_path(module: EvaluatorGoModule, cache_root: Path) -> Path:
    """Return the atomic digest-specific readiness marker for one module cache."""
    return cache_root / f".vibesys-ready.{module.package_digest}"


@dataclass(frozen=True, slots=True)
class TrustedEvaluatorRequirements:
    """Validated package mechanics supplied by product composition."""

    package_root: Path | None = None
    toolchains: frozenset[EvaluatorToolchain] = frozenset()
    tools: Mapping[str, CargoGitToolSpec] = field(default_factory=dict)
    tools_root: Path | None = None
    go_module: EvaluatorGoModule | None = None

    def __post_init__(self) -> None:
        """Snapshot caller-owned mappings and reject impossible declarations."""
        object.__setattr__(self, "tools", MappingProxyType(dict(self.tools)))
        if self.package_root is None and (self.toolchains or self.tools or self.go_module):
            message = (
                "evaluator toolchains, tools, and Go modules require an evaluator package root"
            )
            raise ValueError(message)
        if self.go_module is not None:
            package_root = self.package_root
            if package_root is None:
                message = "evaluator Go module requires an evaluator package root"
                raise ValueError(message)
            try:
                self.go_module.root.relative_to(package_root)
            except ValueError as exc:
                message = "evaluator Go module root must be inside its package"
                raise ValueError(message) from exc


@dataclass(frozen=True, slots=True)
class PreparedTrustedEvaluator:
    """Validated requirements and local dependency roots imported read-only."""

    requirements: TrustedEvaluatorRequirements
    tool_roots: tuple[Path, ...] = ()


def prepare_trusted_evaluator(
    package_root: Path | None,
    evaluator_tools_root: Path,
) -> PreparedTrustedEvaluator:
    """Load one evaluator package and derive its complete host preparation."""
    if package_root is None:
        return PreparedTrustedEvaluator(TrustedEvaluatorRequirements())

    package = load_evaluator_package(package_root)
    tools = package.metadata.tools
    tools_root = evaluator_tools_root if tools else None
    go_module = (
        EvaluatorGoModule(
            root=package.go_module_root,
            cache_root=(
                evaluator_tools_root / "go-modules" / package.digest.removeprefix("sha256:")
            ),
            package_digest=package.digest,
        )
        if package.go_module_root is not None
        else None
    )
    requirements = TrustedEvaluatorRequirements(
        package_root=package.root,
        toolchains=frozenset(package.metadata.toolchains),
        tools=tools,
        tools_root=tools_root,
        go_module=go_module,
    )
    return PreparedTrustedEvaluator(
        requirements,
        (
            *(tool_install_root(evaluator_tools_root, name, spec) for name, spec in tools.items()),
            *((go_module.cache_root,) if go_module is not None else ()),
        ),
    )


@dataclass(frozen=True, slots=True)
class TrustedEvaluationCommandPaths:
    """Concrete path mapping for one trusted evaluator execution target."""

    source_project_root: Path
    runtime_project_root: str
    python_executable: str
    runtime_package_root: str | None = None
    runtime_tools_root: Path | None = None
    runtime_go_module_cache_root: str | None = None


def prepare_trusted_evaluation_plan(
    plan: TrustedEvaluationPlan,
    requirements: TrustedEvaluatorRequirements,
    paths: TrustedEvaluationCommandPaths,
) -> TrustedEvaluationPlan:
    """Translate both commands in *plan* while preserving its execution contract."""
    return plan.model_copy(
        update={
            "accuracy_command": _environment_command(
                plan.accuracy_command,
                requirements=requirements,
                paths=paths,
            ),
            "profile_command": _environment_command(
                plan.profile_command,
                requirements=requirements,
                paths=paths,
            ),
            "benchmark_command": _environment_command(
                plan.benchmark_command,
                requirements=requirements,
                paths=paths,
            ),
        }
    )


def evaluator_container_setup(
    requirements: TrustedEvaluatorRequirements,
    *,
    include_declared_tools: bool = True,
    rootless: bool = False,
) -> list[str]:
    """Return commands that install the evaluator's declared toolchains."""
    if requirements.package_root is None:
        return []
    toolchains = set(requirements.toolchains)
    if include_declared_tools and requirements.tools:
        toolchains.add("rust")
    if not toolchains:
        return []
    commands = (
        [
            "command -v python3 >/dev/null && command -v tar >/dev/null || "
            "{ echo 'evaluator setup requires Python 3 and tar in this remote environment' "
            ">&2; exit 1; }",
            f"mkdir -p .bin {shlex.quote(str(REMOTE_EVALUATOR_TOOLCHAINS_ROOT))}",
            'PATH="$PWD/.bin:$PATH"; export PATH',
        ]
        if rootless
        else [
            "command -v python3 >/dev/null && command -v tar >/dev/null || "
            "{ apt-get update -qq && apt-get install -y -qq python3 ca-certificates tar; }",
        ]
    )
    if not rootless:
        commands.extend(_system_package_commands(requirements))
    if "go" in toolchains:
        go_destination = (
            f"$PWD/{REMOTE_EVALUATOR_TOOLCHAINS_ROOT}/go" if rootless else "/usr/local/go"
        )
        go_link = "$PWD/.bin/go" if rootless else "/usr/local/bin/go"
        go_archive = (
            f"{REMOTE_EVALUATOR_TOOLCHAINS_ROOT}/go.tgz" if rootless else "/tmp/vibesys-go.tgz"  # noqa: S108  # lint-waiver: LW-009095 [S108]; fixed scratch file is private to the isolated setup container and immediately removed.
        )
        go_download = _python_download_command(
            "https://go.dev/dl/go1.23.12.linux-{arch}.tar.gz",
            go_archive,
            architecture_variable="go_arch",
        )
        commands.append(
            "go_version=$(go env GOVERSION 2>/dev/null || true); "
            'case "$go_version" in go1.2[1-9]*|go1.[3-9][0-9]*) ;; *) '
            'arch=$(uname -m); case "$arch" in x86_64) go_arch=amd64 ;; '
            "aarch64|arm64) go_arch=arm64 ;; *) "
            'echo "unsupported Go architecture: $arch" >&2; exit 1 ;; esac; '
            f"{go_download} || "
            "{ echo 'failed to download evaluator Go toolchain' >&2; exit 1; }; "
            f"rm -rf {go_destination} && mkdir -p $(dirname {go_destination}) && "
            f"tar -C $(dirname {go_destination}) -xzf {go_archive} && "
            f"ln -sf {go_destination}/bin/go {go_link} && rm -f {go_archive} || "
            "{ echo 'failed to install evaluator Go toolchain' >&2; exit 1; } ;; esac"
        )
        commands.append("GOWORK=off; export GOWORK")
    if "rust" in toolchains:
        rustup_environment = (
            f"RUSTUP_HOME=$PWD/{REMOTE_EVALUATOR_TOOLCHAINS_ROOT}/rustup "
            f"CARGO_HOME=$PWD/{REMOTE_EVALUATOR_TOOLCHAINS_ROOT}/cargo "
            if rootless
            else ""
        )
        cargo_link = (
            f"ln -sf $PWD/{REMOTE_EVALUATOR_TOOLCHAINS_ROOT}/cargo/bin/* $PWD/.bin/"
            if rootless
            else "ln -sf /root/.cargo/bin/* /usr/local/bin/"
        )
        rustup_init = (
            f"{REMOTE_EVALUATOR_TOOLCHAINS_ROOT}/rustup-init"
            if rootless
            else "/tmp/vibesys-rustup-init"  # noqa: S108  # lint-waiver: LW-009096 [S108]; fixed scratch file is private to the isolated setup container and immediately removed.
        )
        rustup_download = _python_download_command(
            "https://static.rust-lang.org/rustup/dist/{arch}-unknown-linux-gnu/rustup-init",
            rustup_init,
            architecture_variable="rust_arch",
        )
        commands.append(
            "rust_version=$(rustc --version 2>/dev/null | awk '{print $2}' || true); "
            "cargo_version=$(cargo --version 2>/dev/null | awk '{print $2}' || true); "
            'rust_ready=; case "$rust_version" in '
            "1.7[89].*|1.[89][0-9].*|1.[1-9][0-9][0-9].*) "
            'case "$cargo_version" in ?*) rust_ready=1 ;; esac ;; esac; '
            'if [ "$rust_ready" != 1 ]; then '
            'arch=$(uname -m); case "$arch" in x86_64) rust_arch=x86_64 ;; '
            "aarch64|arm64) rust_arch=aarch64 ;; *) "
            'echo "unsupported Rust architecture: $arch" >&2; exit 1 ;; esac; '
            f"{rustup_download} || "
            "{ echo 'failed to download evaluator Rust toolchain' >&2; exit 1; }; "
            f"chmod +x {rustup_init} && {rustup_environment}{rustup_init} "
            "-y --profile minimal --no-modify-path "
            f"--default-toolchain {_EVALUATOR_RUST_TOOLCHAIN_VERSION} && "
            f"{cargo_link} && rm -f {rustup_init} || "
            "{ echo 'failed to install evaluator Rust toolchain' >&2; exit 1; }; fi"
        )
        if rootless:
            commands.append(
                f"if [ -d $PWD/{REMOTE_EVALUATOR_TOOLCHAINS_ROOT}/cargo ]; then "
                f"RUSTUP_HOME=$PWD/{REMOTE_EVALUATOR_TOOLCHAINS_ROOT}/rustup; "
                f"CARGO_HOME=$PWD/{REMOTE_EVALUATOR_TOOLCHAINS_ROOT}/cargo; "
                "export RUSTUP_HOME CARGO_HOME; fi"
            )
    return commands


def _system_package_commands(requirements: TrustedEvaluatorRequirements) -> list[str]:
    """Install the Debian packages the declared tools need to build (root, in a container)."""
    packages = sorted(
        {name for spec in requirements.tools.values() for name in spec.system_packages}
    )
    if not packages:
        return []
    names = shlex.join(packages)
    # Retrying because archive mirrors fail transiently; `dpkg -s` makes a
    # rerun against an image that already has the packages a no-op.
    return [
        f"dpkg -s {names} >/dev/null 2>&1 || {{ "
        "installed=; for attempt in 1 2 3 4 5; do "
        f"if apt-get update -qq && apt-get install -y -qq --no-install-recommends {names}; "
        "then installed=1; break; fi; sleep $((attempt * 5)); done; "
        '[ -n "$installed" ] || '
        f"{{ echo 'failed to install packages for evaluator tools: {names}' >&2; exit 1; }}; }}"
    ]


def remote_evaluator_setup_command(
    requirements: TrustedEvaluatorRequirements,
    *,
    preserve_bootstrap: bool = False,
) -> str | None:
    """Build idempotent setup for a rootless remote evaluator target."""
    commands = evaluator_container_setup(requirements, rootless=True)
    if requirements.tools:
        commands.append(
            evaluator_tools_install_command(requirements.tools, REMOTE_EVALUATOR_TOOLS_ROOT)
        )
    if requirements.go_module is not None:
        commands.append(
            evaluator_go_module_setup_command(
                requirements,
                runtime_package_root=".vibesys-evaluator-package",
                runtime_cache_root=str(REMOTE_EVALUATOR_GO_MODULES_ROOT),
            )
        )
    if not commands:
        return None
    reserved_paths = [
        str(REMOTE_EVALUATOR_TOOLS_ROOT),
        str(REMOTE_EVALUATOR_GO_MODULES_ROOT),
        str(REMOTE_EVALUATOR_TOOLCHAINS_ROOT),
    ]
    if not preserve_bootstrap:
        reserved_paths[:0] = [".bin", ".pip", ".uv-cache"]
    reserved = shlex.join(("rm", "-rf", "--", *reserved_paths))
    return "set -e\n" + "\n".join((reserved, *commands))


def evaluator_go_module_setup_command(
    requirements: TrustedEvaluatorRequirements,
    *,
    runtime_package_root: str,
    runtime_cache_root: str,
) -> str:
    """Return target-side setup that makes one package's Go module offline-ready.

    The online download is part of framework preparation, never gate execution.
    A package-digest receipt turns later preparations into offline verification;
    the final verification deliberately disables both the module proxy and sum
    database so an incomplete cache fails before the evaluator is exposed.
    """
    module = requirements.go_module
    package_root = requirements.package_root
    if module is None or package_root is None:
        message = "evaluator package declares no Go module"
        raise ValueError(message)
    relative_module = module.root.relative_to(package_root).as_posix()
    package_argument = shlex.quote(runtime_package_root)
    cache_argument = shlex.quote(runtime_cache_root)
    relative_argument = shlex.quote(relative_module)
    ready_name = shlex.quote(evaluator_go_module_ready_path(module, Path()).name)
    verify_command = (
        f'python3 -c {shlex.quote(_GO_MODULE_VERIFY_SCRIPT)} "$module_root" "$cache_root"'
    )
    return f"""\
package_root=$(cd -- {package_argument} && pwd) || {{ echo 'evaluator Go module package root is unavailable' >&2; exit 1; }}
module_root="$package_root"/{relative_argument}
cache_path={cache_argument}
cache_parent=$(dirname -- "$cache_path")
mkdir -p -- "$cache_parent" || {{ echo 'cannot create evaluator Go module cache parent' >&2; exit 1; }}
cache_root=$(cd -- "$cache_parent" && pwd)/$(basename -- "$cache_path")
mkdir -p -- "$cache_root" || {{ echo 'cannot create evaluator Go module cache' >&2; exit 1; }}
ready="$cache_root"/{ready_name}
publish_ready=
if [ ! -d "$ready" ]; then
  GOMODCACHE="$cache_root" GOENV=off GOWORK=off GOTOOLCHAIN=local GOFLAGS='-mod=readonly -buildvcs=false' \
    go -C "$module_root" mod download all || {{ echo 'failed to prepare evaluator Go modules; verify go.mod, go.sum, and module-proxy access' >&2; exit 1; }}
  publish_ready=1
fi
GOMODCACHE="$cache_root" GOENV=off GOWORK=off GOTOOLCHAIN=local GOFLAGS='-mod=readonly -buildvcs=false' GOPROXY=off GOSUMDB=off \
  go -C "$module_root" mod verify >/dev/null || {{ echo 'evaluator Go module cache is incomplete after preparation' >&2; exit 1; }}
{verify_command} || {{ echo 'evaluator Go module cache cannot resolve the declared package offline' >&2; exit 1; }}
if [ -n "$publish_ready" ]; then
  mkdir -- "$ready" 2>/dev/null || [ -d "$ready" ] || {{ echo 'cannot publish evaluator Go module cache receipt' >&2; exit 1; }}
fi"""


class EvaluatorGoModuleLifecycleHooks(SandboxLifecycleHooks):
    """Prepare one declared Go module before a host runner becomes ready."""

    def __init__(self, requirements: TrustedEvaluatorRequirements) -> None:
        """Snapshot the immutable package and cache requirement."""
        self._requirements = requirements

    def before_ready(self, context: BeforeReadyContext) -> None:
        """Download and offline-verify module dependencies through the runner."""
        module = self._requirements.go_module
        package_root = self._requirements.package_root
        if module is None or package_root is None:
            return
        command = evaluator_go_module_setup_command(
            self._requirements,
            runtime_package_root=str(package_root),
            runtime_cache_root=str(module.cache_root),
        )
        result = context.sandbox.execute(
            command,
            timeout=_GO_MODULE_SETUP_TIMEOUT_SECONDS,
            output=context.output,
        )
        if result.exit_code != 0:
            detail = (result.output or "Go module setup failed").strip()[-2000:]
            raise EvaluatorGoModuleError(detail)


def evaluator_agent_toolchains(
    requirements: TrustedEvaluatorRequirements,
) -> frozenset[EvaluatorToolchain]:
    """Return toolchains the agent image must bake in for trusted evaluation."""
    toolchains = set(requirements.toolchains)
    if requirements.tools:
        toolchains.add("rust")
    return frozenset(toolchains)


def required_evaluator_tools_root(
    requirements: TrustedEvaluatorRequirements,
    workspace: Path,
) -> Path:
    """Return the operator-owned tools root after enforcing workspace isolation."""
    if requirements.tools_root is None:
        message = "evaluator tools require an operator-owned tools root"
        raise ValueError(message)
    root = requirements.tools_root.resolve()
    try:
        root.relative_to(workspace.resolve())
    except ValueError:
        return root
    message = "evaluator tools root must be outside the candidate workspace"
    raise ValueError(message)


def required_evaluator_go_module_cache_root(
    requirements: TrustedEvaluatorRequirements,
    workspace: Path,
) -> Path:
    """Return the operator-owned Go cache after enforcing workspace isolation."""
    if requirements.go_module is None:
        message = "evaluator package declares no Go module cache"
        raise ValueError(message)
    root = requirements.go_module.cache_root.resolve()
    try:
        root.relative_to(workspace.resolve())
    except ValueError:
        return root
    message = "evaluator Go module cache must be outside the candidate workspace"
    raise ValueError(message)


def docker_evaluator_tools_root(
    requirements: TrustedEvaluatorRequirements,
    workspace: Path,
    *,
    image_identity: str,
) -> Path:
    """Return the target-image-specific evaluator tool cache root."""
    base = required_evaluator_tools_root(requirements, workspace)
    identity = (
        f"{_DOCKER_EVALUATOR_CACHE_SCHEMA}\0{image_identity}\0{os.uname().machine}\0"
        f"{_EVALUATOR_RUST_TOOLCHAIN_VERSION}"
    ).encode()
    return base / "docker" / hashlib.sha256(identity).hexdigest()


def _environment_command(
    command: str | None,
    *,
    requirements: TrustedEvaluatorRequirements,
    paths: TrustedEvaluationCommandPaths,
) -> str | None:
    if command is None:
        return None
    try:
        arguments = shlex.split(command)
    except ValueError as exc:
        message = f"invalid evaluator command: {exc}"
        raise ValueError(message) from exc
    replacements = [
        (PROJECT_ROOT_TOKEN, paths.runtime_project_root),
        (PYTHON_TOKEN, paths.python_executable),
    ]
    if requirements.package_root is not None:
        runtime_package_root = paths.runtime_package_root
        if runtime_package_root is None:
            message = "evaluator package execution requires a runtime package root"
            raise ValueError(message)
        replacements.append((str(requirements.package_root), runtime_package_root))
    if requirements.tools:
        tools_root = paths.runtime_tools_root or required_evaluator_tools_root(
            requirements,
            paths.source_project_root,
        )
        replacements.extend(tool_path_replacements(requirements.tools, tools_root).items())
    translated = translate_command_arguments(arguments, replacements)
    if requirements.go_module is not None:
        cache_root = paths.runtime_go_module_cache_root
        if cache_root is None:
            message = "evaluator Go module execution requires a runtime cache root"
            raise ValueError(message)
        environment = (
            "env",
            f"GOMODCACHE={cache_root}",
            "GOPROXY=off",
            "GOSUMDB=off",
            "GOWORK=off",
            "GOENV=off",
            "GOTOOLCHAIN=local",
            "GOFLAGS=-mod=readonly -buildvcs=false",
        )
        if Path(cache_root).is_absolute():
            translated = (*environment, *translated)
        else:
            script = (
                'cache_path=$1; shift; cache_parent=$(dirname -- "$cache_path"); '
                'cache_root=$(cd -- "$cache_parent" && pwd)/$(basename -- "$cache_path") || exit; '
                'exec env "GOMODCACHE=$cache_root" GOPROXY=off GOSUMDB=off GOWORK=off '
                "GOENV=off GOTOOLCHAIN=local 'GOFLAGS=-mod=readonly -buildvcs=false' \"$@\""
            )
            translated = (
                "sh",
                "-c",
                script,
                "vibesys-offline-go",
                cache_root,
                *translated,
            )
    return shlex.join(translated)


def _python_download_command(
    url_template: str,
    destination: str,
    *,
    architecture_variable: str,
) -> str:
    command = shlex.join(("python3", "-c", _PYTHON_DOWNLOAD_SCRIPT, url_template, destination))
    return f'{command} "${{{architecture_variable}}}"'
