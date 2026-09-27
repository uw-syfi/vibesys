"""Prepare trusted evaluator commands for one concrete execution environment."""

from __future__ import annotations

import hashlib
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Literal

from vs_runtime._evaluator_packages import load_evaluator_package
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
REMOTE_EVALUATOR_TOOLS_ROOT = Path(".vibesys-evaluator-tools")
REMOTE_EVALUATOR_TOOLCHAINS_ROOT = Path(".vibesys-evaluator-toolchains")

_EVALUATOR_RUST_TOOLCHAIN_VERSION = "1.92.0"
_DOCKER_EVALUATOR_CACHE_SCHEMA = 2
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


@dataclass(frozen=True, slots=True)
class TrustedEvaluatorRequirements:
    """Validated package mechanics supplied by product composition."""

    package_root: Path | None = None
    toolchains: frozenset[EvaluatorToolchain] = frozenset()
    tools: Mapping[str, CargoGitToolSpec] = field(default_factory=dict)
    tools_root: Path | None = None

    def __post_init__(self) -> None:
        """Snapshot caller-owned mappings and reject impossible declarations."""
        object.__setattr__(self, "tools", MappingProxyType(dict(self.tools)))
        if self.package_root is None and (self.toolchains or self.tools):
            message = "evaluator toolchains and tools require an evaluator package root"
            raise ValueError(message)


@dataclass(frozen=True, slots=True)
class PreparedTrustedEvaluator:
    """Validated evaluator requirements and declared local tool roots."""

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
    requirements = TrustedEvaluatorRequirements(
        package_root=package.root,
        toolchains=frozenset(package.metadata.toolchains),
        tools=tools,
        tools_root=tools_root,
    )
    return PreparedTrustedEvaluator(
        requirements,
        tuple(tool_install_root(evaluator_tools_root, name, spec) for name, spec in tools.items()),
    )


@dataclass(frozen=True, slots=True)
class TrustedEvaluationCommandPaths:
    """Concrete path mapping for one trusted evaluator execution target."""

    source_project_root: Path
    runtime_project_root: str
    python_executable: str
    runtime_package_root: str | None = None
    runtime_tools_root: Path | None = None


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
    if not commands:
        return None
    reserved_paths = [
        str(REMOTE_EVALUATOR_TOOLS_ROOT),
        str(REMOTE_EVALUATOR_TOOLCHAINS_ROOT),
    ]
    if not preserve_bootstrap:
        reserved_paths[:0] = [".bin", ".pip", ".uv-cache"]
    reserved = shlex.join(("rm", "-rf", "--", *reserved_paths))
    return "set -e\n" + "\n".join((reserved, *commands))


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
    return shlex.join(translate_command_arguments(arguments, replacements))


def _python_download_command(
    url_template: str,
    destination: str,
    *,
    architecture_variable: str,
) -> str:
    command = shlex.join(("python3", "-c", _PYTHON_DOWNLOAD_SCRIPT, url_template, destination))
    return f'{command} "${{{architecture_variable}}}"'
