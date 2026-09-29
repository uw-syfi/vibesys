"""Per-server dependencies and the file and git primitives the endpoints share."""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel, JsonValue, ValidationError

from entrypoints.web_home.contract import ApiError, ErrorCode

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

_ModelT = TypeVar("_ModelT", bound=BaseModel)
_GIT_TIMEOUT_SECONDS = 60
_GIT_TERM_GRACE_SECONDS = 5
_GIT_OVERRIDES = frozenset({"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"})
_PORCELAIN_STATUS_WIDTH = len("XY ")
_KEYCHAIN_TIMEOUT_SECONDS = 2
_KEYCHAIN_ITEM_NOT_FOUND = 44


def _utc_now() -> datetime:
    return datetime.now(UTC)


def keychain_has(service: str) -> bool | None:
    """Return whether the macOS keychain holds a *service* item, without reading its secret.

    ``None`` means unknown: not macOS, no ``security`` tool, a timeout, or an
    unexpected status. Without ``-w`` the tool prints attributes only, and the
    output is discarded.
    """
    executable = shutil.which("security") if sys.platform == "darwin" else None
    if executable is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603  # lint-waiver: LW-101307 [S603]; fixed `security` lookup argv with a constant service name, never a shell string.
            # > A keychain binding would be a new dependency for one presence check;
            # > shell=True would weaken argv safety.
            [executable, "find-generic-password", "-s", service],
            capture_output=True,
            timeout=_KEYCHAIN_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0:
        return True
    return False if result.returncode == _KEYCHAIN_ITEM_NOT_FOUND else None


@dataclass
class HomeConfig:
    """Everything the endpoints read from the host; tests inject each field."""

    state_home: Path
    roots: tuple[Path, ...]
    dotenv_path: Path
    assets_dir: Path | None
    port: int
    dev_origins: tuple[str, ...] = ()
    environ: Mapping[str, str] = field(default_factory=lambda: os.environ)
    clock: Callable[[], datetime] = _utc_now
    keychain: Callable[[str], bool | None] = keychain_has
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    # ponytail: one lock serializes every file write; per-file locks if it contends.
    write_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def origin(self) -> str:
        """Return the exact origin the app is served from."""
        return f"http://127.0.0.1:{self.port}"


@dataclass(frozen=True)
class Request:
    """One routed API request."""

    config: HomeConfig
    params: tuple[str, ...]
    query: dict[str, list[str]]
    body: bytes

    def arg(self, name: str) -> str | None:
        """Return the first value of one query parameter."""
        values = self.query.get(name)
        return values[0] if values else None


def validation_errors(error: ValidationError) -> list[JsonValue]:
    """Describe validation failures by location and message, never by input value."""
    return [
        {"loc": [str(part) for part in item["loc"]], "msg": item["msg"]}
        for item in error.errors(include_input=False, include_url=False)
    ]


def parse_body(request: Request, model: type[_ModelT]) -> _ModelT:
    """Validate a JSON body; the error never echoes a submitted value."""
    try:
        return model.model_validate_json(request.body or b"{}")
    except ValidationError as error:
        message = "request body is invalid"
        raise ApiError(
            ErrorCode.INVALID_REQUEST, message, details={"errors": validation_errors(error)}
        ) from None


def atomic_write(path: Path, data: bytes, *, mode: int) -> None:
    """Replace *path* with *data* by one rename; a symlinked *path* is refused."""
    if path.is_symlink():
        message = f"refusing to replace a symlink: {path}"
        raise ApiError(ErrorCode.SYMLINK_REJECTED, message)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def git(
    root: Path, *arguments: str, timeout: float = _GIT_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[str]:
    """Run one git command in *root* without a shell or inherited repository overrides.

    On a timeout, git is asked to exit (SIGTERM) and given a grace period
    before being killed. A SIGKILL would skip git's lockfile cleanup and leave
    ``.git/index.lock`` behind, wedging the repository for every later git call,
    including the user's own.
    """
    executable = shutil.which("git")
    if executable is None:
        message = "git is not installed"
        raise ApiError(ErrorCode.NOT_GIT, message)
    environment = {key: value for key, value in os.environ.items() if key not in _GIT_OVERRIDES}
    process = subprocess.Popen(  # noqa: S603  # lint-waiver: LW-101302 [S603]; git argv is built here from fixed subcommands and validated paths, never a shell string.
        # > Routing through GitTracker needs a run id and state integration the
        # > setup API does not have; shell=True would weaken argv safety.
        [executable, *arguments],
        cwd=root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.communicate(timeout=_GIT_TERM_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        message = f"git did not respond within {timeout}s"
        raise ApiError(ErrorCode.INTERNAL, message) from None
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def pending_changes(root: Path) -> list[str]:
    """Return changed and untracked paths under *root*, relative to it.

    Same query as ``GitTracker.pending_changes``, which launch uses to reject a
    dirty tree, so the setup API and launch agree on what "dirty" means.
    """
    prefix = git(root, "rev-parse", "--show-prefix").stdout.strip()
    records = git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--", ".")
    fields = records.stdout.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if len(record) <= _PORCELAIN_STATUS_WIDTH:
            continue
        paths.append(record[_PORCELAIN_STATUS_WIDTH:].removeprefix(prefix))
        if record[0] in "RC":
            index += 1
    return sorted(paths)
