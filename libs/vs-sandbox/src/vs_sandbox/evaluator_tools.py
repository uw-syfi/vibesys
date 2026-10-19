"""Install immutable external tools declared by evaluator packages."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, field_validator

from vs_decode.api import Unreadable, read_json
from vs_sandbox.lifecycle import BeforeReadyContext, SandboxLifecycleHooks

#: What one tool command returns: captured text streams and an exit status.
ToolResult = subprocess.CompletedProcess[str]
ToolCommandRunner = Callable[[Sequence[str]], ToolResult]


def tool_result(
    argv: Sequence[str], returncode: int = 0, stdout: str = "", stderr: str = ""
) -> ToolResult:
    """Build the result a tool command runner returns for *argv* (a Fake runner's answer)."""
    return subprocess.CompletedProcess(list(argv), returncode, stdout, stderr)


def tool_timed_out(argv: Sequence[str], seconds: float) -> Exception:
    """Build the error a tool command runner raises when *argv* outlives *seconds*."""
    return subprocess.TimeoutExpired(list(argv), seconds)


_SANDBOX_INSTALL_TIMEOUT_SECONDS = 660
_MAX_INSTALL_ERROR_CHARACTERS = 2000
_TOOL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
_CARGO_IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9]+(?:[-_][a-z0-9]+)*$")
_GIT_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40}$")
_DEBIAN_PACKAGE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9+.-]+$")


class CargoGitToolSpec(BaseModel):
    """One Cargo package installed from an immutable Git revision.

    ``system_packages`` names the Debian/Ubuntu packages the build needs beyond
    Rust and a linker (for example ``libssl-dev`` for a crate that links
    OpenSSL). It is the single declaration of the tool's native build
    dependencies: a Docker target installs exactly these with ``apt-get`` before
    building, and other targets must already provide them.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["cargo-git"]
    git: str
    rev: str
    package: str
    bins: tuple[str, ...]
    system_packages: tuple[str, ...] = ()

    @field_validator("system_packages")
    @classmethod
    def _valid_system_packages(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) != len(set(value)):
            message = "system_packages must not contain duplicates"
            raise ValueError(message)
        invalid = next(
            (name for name in value if not _DEBIAN_PACKAGE_PATTERN.fullmatch(name)),
            None,
        )
        if invalid is not None:
            message = f"invalid Debian package name: {invalid!r}"
            raise ValueError(message)
        return value

    @field_validator("git")
    @classmethod
    def _valid_git_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            message = "git must be an HTTPS URL without credentials, query, or fragment"
            raise ValueError(message)
        return value

    @field_validator("rev")
    @classmethod
    def _full_git_revision(cls, value: str) -> str:
        if not _GIT_REVISION_PATTERN.fullmatch(value):
            message = "rev must be a full 40-character lowercase Git commit SHA"
            raise ValueError(message)
        return value

    @field_validator("package")
    @classmethod
    def _valid_package(cls, value: str) -> str:
        if not _CARGO_IDENTIFIER_PATTERN.fullmatch(value):
            message = "package must be a canonical Cargo package name"
            raise ValueError(message)
        return value

    @field_validator("bins")
    @classmethod
    def _valid_bins(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            message = "bins must declare at least one binary"
            raise ValueError(message)
        if len(value) != len(set(value)):
            message = "bins must not contain duplicates"
            raise ValueError(message)
        invalid = next(
            (binary for binary in value if not _CARGO_IDENTIFIER_PATTERN.fullmatch(binary)),
            None,
        )
        if invalid is not None:
            message = f"invalid Cargo binary name: {invalid!r}"
            raise ValueError(message)
        return value


def tool_token(tool: str, binary: str) -> str:
    """Return the semantic argv token for one declared evaluator tool binary."""
    return f"${{TOOL:{tool}/{binary}}}"


_TARGET_INSTALL_PROGRAM = r"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CARGO_TIMEOUT = 600
MAX_ERROR_CHARACTERS = 2000
TOOL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")


class InstallError(RuntimeError):
    pass


def file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as binary:
        while chunk := binary.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def output_tail(stream):
    maximum_bytes = MAX_ERROR_CHARACTERS * 4
    stream.seek(0, os.SEEK_END)
    stream.seek(max(0, stream.tell() - maximum_bytes))
    return stream.read(maximum_bytes).decode("utf-8", errors="replace")[
        -MAX_ERROR_CHARACTERS:
    ]


def bounded_detail(value):
    detail = value.strip()
    if len(detail) <= MAX_ERROR_CHARACTERS:
        return detail
    marker = "\n... output truncated ...\n"
    head_length = min(256, MAX_ERROR_CHARACTERS - len(marker))
    tail_length = MAX_ERROR_CHARACTERS - len(marker) - head_length
    return detail[:head_length] + marker + detail[-tail_length:]


def binary_hashes(root, spec):
    hashes = {}
    for binary in spec["bins"]:
        binary_path = root / "bin" / binary
        if (
            binary_path.is_symlink()
            or not binary_path.is_file()
            or not os.access(binary_path, os.X_OK)
        ):
            return None
        hashes[binary] = file_sha256(binary_path)
    return hashes


def verified_install(root, spec):
    if root.is_symlink():
        return False
    receipt_path = root / "receipt.json"
    if receipt_path.is_symlink():
        return False
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    hashes = binary_hashes(root, spec)
    expected = {"schema_version": 1, "spec": spec, "binaries": hashes}
    return hashes is not None and receipt == expected


def write_receipt(root, spec, binaries):
    descriptor, temporary_name = tempfile.mkstemp(prefix=".receipt-", dir=root)
    temporary = Path(temporary_name)
    try:
        document = json.dumps(
            {"schema_version": 1, "spec": spec, "binaries": binaries},
            sort_keys=True,
            separators=(",", ":"),
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(document + "\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(root / "receipt.json")
    finally:
        temporary.unlink(missing_ok=True)


def make_install_readable(root, spec):
    (root / "receipt.json").chmod(0o444)
    (root / "bin").chmod(0o555)
    for binary in spec["bins"]:
        (root / "bin" / binary).chmod(0o555)
    root.chmod(0o555)


def search_directories(login_path):
    # The directories cargo is searched in: the login shell's PATH, then this process's.
    directories = []
    for path in (login_path, os.environ.get("PATH", "")):
        for directory in path.split(os.pathsep):
            if directory and directory not in directories:
                directories.append(directory)
    return directories


def find_cargo(directories):
    for directory in directories:
        candidate = Path(directory) / "cargo"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def cargo_missing_message(name, directories):
    searched = os.pathsep.join(directories) or "(empty)"
    return (
        f"cannot install evaluator tool {name!r}: cargo was not found on the PATH searched "
        f"({searched}). Install Rust for this user (rustup), or put cargo's directory on "
        "the PATH of the user's login shell, for example by sourcing $HOME/.cargo/env from "
        "~/.profile; VibeSys searches that PATH as well as its own."
    )


def install_tool(name, spec, target, directories):
    cargo = find_cargo(directories)
    if cargo is None:
        raise InstallError(cargo_missing_message(name, directories))
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    try:
        binary_arguments = [
            argument
            for binary in spec["bins"]
            for argument in ("--bin", binary)
        ]
        arguments = [
            cargo,
            "install",
            "--git",
            spec["git"],
            "--rev",
            spec["rev"],
            "--locked",
            "--root",
            str(staging),
            *binary_arguments,
            spec["package"],
        ]
        with (
            tempfile.TemporaryDirectory(prefix="vibesys-cargo-home-") as cargo_home,
            tempfile.TemporaryDirectory(prefix="vibesys-cargo-work-") as cargo_work,
            tempfile.TemporaryFile() as stdout,
            tempfile.TemporaryFile() as stderr,
        ):
            cargo_environment = {
                key: value
                for key, value in os.environ.items()
                if not key.startswith(("CARGO_", "GIT_CONFIG", "RUST"))
                and key not in {"GIT_DIR", "GIT_WORK_TREE"}
            }
            cargo_environment["CARGO_HOME"] = cargo_home
            cargo_environment["PATH"] = os.pathsep.join(directories)
            if "RUSTUP_HOME" in os.environ:
                cargo_environment["RUSTUP_HOME"] = os.environ["RUSTUP_HOME"]
            try:
                result = subprocess.run(
                    arguments,
                    check=False,
                    cwd=cargo_work,
                    env=cargo_environment,
                    stdout=stdout,
                    stderr=stderr,
                    timeout=CARGO_TIMEOUT,
                )
            except FileNotFoundError as exc:
                raise InstallError(cargo_missing_message(name, directories)) from exc
            except subprocess.TimeoutExpired as exc:
                raise InstallError(
                    f"cannot install evaluator tool {name!r}: cargo install timed out"
                ) from exc
            stderr_detail = output_tail(stderr)
            stdout_detail = output_tail(stdout)
        if result.returncode != 0:
            detail = bounded_detail(stderr_detail or stdout_detail or "cargo install failed")
            raise InstallError(f"cannot install evaluator tool {name!r}: {detail}")
        binaries = binary_hashes(staging, spec)
        if binaries is None:
            detail = bounded_detail(stderr_detail or stdout_detail)
            suffix = f": {detail}" if detail else ""
            raise InstallError(
                f"cargo did not install every declared binary for evaluator tool {name!r}"
                + suffix
            )
        write_receipt(staging, spec, binaries)
        published = False
        try:
            staging.replace(target)
            published = True
        except OSError as exc:
            if not target.exists() or not verified_install(target, spec):
                raise InstallError(
                    f"cannot publish evaluator tool installation: {target}"
                ) from exc
        if published:
            make_install_readable(target, spec)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def main():
    document = json.loads(sys.argv[1])
    if (
        not isinstance(document, dict)
        or set(document) != {"schema_version", "tools"}
        or document["schema_version"] != 1
        or not isinstance(document["tools"], dict)
    ):
        raise InstallError("invalid evaluator tool manifest")
    install_parent = Path(sys.argv[2])
    if not install_parent.is_absolute():
        install_parent = Path.cwd() / install_parent
    if install_parent.is_symlink():
        raise InstallError(f"evaluator tool install root is a symlink: {install_parent}")
    install_parent.mkdir(parents=True, exist_ok=True)
    directories = search_directories(sys.argv[3] if len(sys.argv) > 3 else "")
    for name, spec in document["tools"].items():
        if not isinstance(name, str) or TOOL_NAME_PATTERN.fullmatch(name) is None:
            raise InstallError(f"invalid evaluator tool name: {name!r}")
        encoded = json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
        tool_parent = install_parent / name
        if tool_parent.is_symlink():
            raise InstallError(f"evaluator tool cache path is a symlink: {tool_parent}")
        target = tool_parent / hashlib.sha256(encoded).hexdigest()
        if verified_install(target, spec):
            continue
        if target.exists():
            raise InstallError(
                f"evaluator tool installation failed receipt verification: {target}"
            )
        install_tool(name, spec, target, directories)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        detail = bounded_detail(str(exc) or type(exc).__name__)
        print(detail, file=sys.stderr)
        raise SystemExit(1) from None
""".strip()


class EvaluatorToolError(RuntimeError):
    """Raised when an evaluator tool cannot be prepared safely."""

    @classmethod
    def cache_ownership_failed(cls, detail: str) -> EvaluatorToolError:
        """Describe a tool-cache ownership change that failed in Docker."""
        return cls(
            "Docker evaluator tool builder could not return cache ownership "
            f"to the host user: {detail}"
        )

    @classmethod
    def builder_setup_failed(cls, detail: str) -> EvaluatorToolError:
        """Report a failed root setup command in the evaluator tool builder."""
        return cls(f"evaluator tool builder setup failed: {detail}")

    @classmethod
    def builder_incomplete(cls) -> EvaluatorToolError:
        """Describe a builder that failed to publish every declared tool."""
        return cls("Docker evaluator tool builder did not publish every declared tool")

    @classmethod
    def docker_image_unconfigured(cls) -> EvaluatorToolError:
        """Describe Docker execution without a configured backend image."""
        return cls("Docker execution requires a configured backend image")

    @classmethod
    def sandbox_installation_failed(cls, exit_code: int | None, details: str) -> EvaluatorToolError:
        """Describe a nonzero exit from the target-side tool installer."""
        return cls(f"evaluator tool sandbox installation failed (exit {exit_code}): {details}")

    @classmethod
    def receipt_verification_failed(cls, target: Path) -> EvaluatorToolError:
        """Describe a tool cache entry whose receipt does not verify."""
        return cls(f"evaluator tool installation failed receipt verification: {target}")

    @classmethod
    def cargo_missing(cls, name: str) -> EvaluatorToolError:
        """Describe Cargo being absent while installing a named tool."""
        return cls(f"cannot install evaluator tool {name!r}: cargo was not found")

    @classmethod
    def cargo_install_timed_out(cls, name: str) -> EvaluatorToolError:
        """Describe Cargo installation exceeding its timeout."""
        return cls(f"cannot install evaluator tool {name!r}: cargo install timed out")

    @classmethod
    def cargo_install_failed(cls, name: str, details: str) -> EvaluatorToolError:
        """Describe Cargo failing to install a named tool."""
        return cls(f"cannot install evaluator tool {name!r}: {details}")

    @classmethod
    def declared_binaries_missing(cls, name: str, suffix: str) -> EvaluatorToolError:
        """Describe an install that did not produce all declared executables."""
        return cls(
            f"cargo did not install every declared binary for evaluator tool {name!r}{suffix}"
        )

    @classmethod
    def install_publish_failed(cls, target: Path) -> EvaluatorToolError:
        """Describe a staged tool installation that could not be published."""
        return cls(f"cannot publish evaluator tool installation: {target}")


class EvaluatorToolLifecycleHooks(SandboxLifecycleHooks):
    """Install evaluator-declared tools while a sandbox becomes ready."""

    def __init__(
        self,
        tools: Mapping[str, CargoGitToolSpec],
        install_parent: Path,
        *,
        search_path: str = "",
    ) -> None:
        """Snapshot immutable tool requirements and the target-native install root.

        *search_path* is a ``PATH`` the target searches for ``cargo`` before its own
        (the user's login shell's, for a host target); empty means the target's own.
        """
        self._tools = dict(tools)
        self._install_parent = install_parent
        self._search_path = search_path

    def before_ready(self, context: BeforeReadyContext) -> None:
        """Prepare verified tools before the sandbox is exposed to callers."""
        command = evaluator_tools_install_command(
            self._tools,
            self._install_parent,
            search_path=self._search_path,
        )
        result = context.sandbox.execute(
            command,
            timeout=_SANDBOX_INSTALL_TIMEOUT_SECONDS,
            output=context.output,
        )
        if result.exit_code != 0:
            detail = _bounded_install_detail(result.output or "target-side installer failed")
            raise EvaluatorToolError.sandbox_installation_failed(result.exit_code, detail)


class _EvaluatorToolReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1]
    spec: CargoGitToolSpec
    binaries: dict[str, str]


def evaluator_tools_install_command(
    tools: Mapping[str, CargoGitToolSpec],
    install_parent: str | Path,
    *,
    search_path: str = "",
) -> str:
    """Return a safely quoted command that prepares tools in an execution target.

    The installer looks for ``cargo`` only when a tool is not already installed and
    verified, first in *search_path* (when given) and then in the target's own
    ``PATH``; when it finds none, it fails naming the tool, the directories searched
    and the fix.

    The target needs only ``python3``, Cargo, and the standard library. The
    generated command verifies content-addressed cache hits, installs missing
    tools through atomic staging directories, and records executable hashes in
    versioned receipts.
    """
    invalid_name = next(
        (
            name
            for name in tools
            if not isinstance(name, str) or _TOOL_NAME_PATTERN.fullmatch(name) is None
        ),
        None,
    )
    if invalid_name is not None:
        message = f"invalid evaluator tool name: {invalid_name!r}"
        raise ValueError(message)
    document = {
        "schema_version": 1,
        "tools": {name: spec.model_dump(mode="json") for name, spec in sorted(tools.items())},
    }
    payload = json.dumps(document, sort_keys=True, separators=(",", ":"))
    return shlex.join(
        ("python3", "-c", _TARGET_INSTALL_PROGRAM, payload, str(install_parent), search_path)
    )


def cargo_install_argv(spec: CargoGitToolSpec, install_root: Path) -> tuple[str, ...]:
    """Build the exact Cargo invocation for an immutable Git tool."""
    binary_arguments = tuple(argument for binary in spec.bins for argument in ("--bin", binary))
    return (
        "cargo",
        "install",
        "--git",
        spec.git,
        "--rev",
        spec.rev,
        "--locked",
        "--root",
        str(install_root),
        *binary_arguments,
        spec.package,
    )


def prepare_evaluator_tools(
    tools: Mapping[str, CargoGitToolSpec],
    install_parent: Path,
    *,
    command_runner: ToolCommandRunner,
) -> dict[str, str]:
    """Install evaluator tools under a trusted content-addressed cache and return paths.

    A tool whose verified receipt is missing is installed by *command_runner*,
    which runs :func:`cargo_install_argv` where the tools are built; a
    production caller builds them in a target sandbox instead and passes a
    runner that refuses, so this only verifies what the sandbox published.
    """
    runner = command_runner
    install_parent.mkdir(parents=True, exist_ok=True)
    replacements: dict[str, str] = {}
    for name, spec in tools.items():
        target = tool_install_root(install_parent, name, spec)
        if not _verified_install(target, spec):
            if target.exists():
                raise EvaluatorToolError.receipt_verification_failed(target)
            _install_tool(name, spec, target, runner)
        replacements.update(tool_path_replacements({name: spec}, install_parent))
    return replacements


def tool_path_replacements(
    tools: Mapping[str, CargoGitToolSpec], install_parent: Path
) -> dict[str, str]:
    """Map semantic tool tokens to binary paths below ``install_parent``."""
    return {
        tool_token(name, binary): str(
            tool_install_root(install_parent, name, spec) / "bin" / binary
        )
        for name, spec in tools.items()
        for binary in spec.bins
    }


def tool_spec_digest(spec: CargoGitToolSpec) -> str:
    """Return the content key for one normalized tool specification."""
    document = spec.model_dump(mode="json")
    encoded = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def tool_install_root(install_parent: Path, name: str, spec: CargoGitToolSpec) -> Path:
    """Return the immutable cache root selected by the canonical tool specification."""
    return install_parent / name / tool_spec_digest(spec)


def _install_tool(
    name: str,
    spec: CargoGitToolSpec,
    target: Path,
    runner: ToolCommandRunner,
) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    try:
        try:
            result = runner(cargo_install_argv(spec, staging))
        except FileNotFoundError as exc:
            raise EvaluatorToolError.cargo_missing(name) from exc
        except subprocess.TimeoutExpired as exc:
            raise EvaluatorToolError.cargo_install_timed_out(name) from exc
        if result.returncode != 0:
            detail = _bounded_install_detail(
                result.stderr or result.stdout or "cargo install failed"
            )
            raise EvaluatorToolError.cargo_install_failed(name, detail)
        binaries = _installed_binary_hashes(staging, spec)
        if binaries is None:
            detail = _bounded_install_detail(result.stderr or result.stdout)
            suffix = f": {detail}" if detail else ""
            raise EvaluatorToolError.declared_binaries_missing(name, suffix)
        _write_receipt(
            staging,
            _EvaluatorToolReceipt(schema_version=1, spec=spec, binaries=binaries),
        )
        try:
            staging.replace(target)
        except OSError as exc:
            if not target.exists() or not _verified_install(target, spec):
                raise EvaluatorToolError.install_publish_failed(target) from exc
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _bounded_install_detail(value: str) -> str:
    detail = value.strip()
    if len(detail) <= _MAX_INSTALL_ERROR_CHARACTERS:
        return detail
    marker = "\n... output truncated ...\n"
    head_length = min(256, _MAX_INSTALL_ERROR_CHARACTERS - len(marker))
    tail_length = _MAX_INSTALL_ERROR_CHARACTERS - len(marker) - head_length
    return detail[:head_length] + marker + detail[-tail_length:]


def _installed_binary_hashes(root: Path, spec: CargoGitToolSpec) -> dict[str, str] | None:
    hashes: dict[str, str] = {}
    for binary in spec.bins:
        binary_path = root / "bin" / binary
        if (
            binary_path.is_symlink()
            or not binary_path.is_file()
            or not os.access(binary_path, os.X_OK)
        ):
            return None
        hashes[binary] = _file_sha256(binary_path)
    return hashes


_MAX_RECEIPT_BYTES = 1024 * 1024
"""The largest installed-tool receipt read back."""


def _verified_install(root: Path, spec: CargoGitToolSpec) -> bool:
    if root.is_symlink():
        return False
    receipt_path = root / "receipt.json"
    if receipt_path.is_symlink():
        return False
    receipt = read_json(
        receipt_path, _EvaluatorToolReceipt, max_bytes=_MAX_RECEIPT_BYTES, follow_symlinks=False
    )
    if isinstance(receipt, Unreadable):
        return False
    hashes = _installed_binary_hashes(root, spec)
    return receipt.spec == spec and hashes is not None and receipt.binaries == hashes


def _write_receipt(root: Path, receipt: _EvaluatorToolReceipt) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".receipt-", dir=root)
    temporary = Path(temporary_name)
    try:
        document = json.dumps(
            receipt.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(f"{document}\n")
            output.flush()
            os.fsync(output.fileno())
        temporary.replace(root / "receipt.json")
    finally:
        temporary.unlink(missing_ok=True)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as binary:
        while chunk := binary.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
