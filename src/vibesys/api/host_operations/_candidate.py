"""Read-only inspection of one candidate directory.

The gateway chooses the executable and argv. This module only interprets the
files a checkout holds: its project declaration, helper entrypoint, task
directory and git HEAD. It does not launch a command.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from vs_proto.api import API_SCHEMA_HASH
from vs_proto.api import host_payloads as pb

_RELEASE = re.compile(r"^(\d+(?:\.\d+)*)")
_PYPROJECT_LIMIT_BYTES = 1 << 20
_PATH_LIMIT_BYTES = 4096
_COMMIT_HEX_LENGTH = 40
_MINIMUM = (0, 1, 1)
MINIMUM_VERSION = "0.1.1"
_HELPER_ENTRY = Path("src") / "entrypoints" / "host_helper.py"


def release_of(version: str) -> tuple[int, ...] | None:
    """The release segment of ``version``, or ``None`` when it has none."""
    match = _RELEASE.match(version)
    return None if match is None else tuple(int(part) for part in match.group(1).split("."))


def inspect_candidate(request: pb.InspectCandidateRequest) -> pb.InspectCandidateResponse:
    """Describe ``request.path`` and echo its selection revision."""
    root = _path(request.path)
    response = pb.InspectCandidateResponse(
        request_id=request.request_id,
        selection_revision=request.selection_revision,
        schema_hash=API_SCHEMA_HASH,
        state=pb.CandidateState.NOT_FOUND,
        minimum_version=MINIMUM_VERSION,
    )
    try:
        root.stat()
    except FileNotFoundError:
        return response
    except PermissionError:
        raise
    except OSError as exc:
        message = f"candidate path cannot be read: {exc}"
        raise ValueError(message) from exc
    try:
        version = _vibesys_version(root / "pyproject.toml")
    except PermissionError:
        raise
    except OSError as exc:
        message = f"candidate path cannot be read: {exc}"
        raise ValueError(message) from exc
    response.git_repository = _exists(root / ".git")
    response.vibesys_project = _is_dir(root / ".vibesys" / "tasks")
    if version is None:
        return response
    response.vibesys_checkout = True
    release = release_of(version)
    identity = pb.CandidateIdentity(
        root=str(root),
        version=version,
        compatible=(root / _HELPER_ENTRY).is_file(),
        vibesys_project=response.vibesys_project,
        git_repository=response.git_repository,
    )
    commit = _commit(root)
    if commit is not None:
        identity.commit = commit
    if release is None or release < _MINIMUM:
        response.state = pb.CandidateState.TOO_OLD
    else:
        response.state = pb.CandidateState.FOUND
    response.identity = identity
    return response


def _path(text: str) -> Path:
    if any(char in text for char in "\x00\n\r") or len(text.encode()) > _PATH_LIMIT_BYTES:
        message = "candidate path is not usable"
        raise ValueError(message)
    if text == "~" or text.startswith("~/"):
        rest = "" if text == "~" else text[2:]
        return (Path.home() / rest).resolve()
    path = Path(text)
    if not path.is_absolute():
        path = Path.home() / path
    return path.resolve()


def _vibesys_version(pyproject: Path) -> str | None:
    try:
        if pyproject.stat().st_size > _PYPROJECT_LIMIT_BYTES:
            return None
        with pyproject.open("rb") as handle:
            document = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None
    project = document.get("project")
    if not isinstance(project, dict) or project.get("name") != "vibesys":
        return None
    version = project.get("version")
    return version if isinstance(version, str) else ""


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _commit(root: Path) -> str | None:
    git = root / ".git"
    try:
        head = git / "HEAD" if git.is_dir() else None
        if head is None or not head.is_file():
            return None
        line = head.read_text(encoding="utf-8").strip()
        if line.startswith("ref: "):
            ref = (git / line.removeprefix("ref: ").strip()).resolve()
            if not ref.is_relative_to(git.resolve()):
                return None
            line = ref.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError, ValueError):
        return None
    if len(line) == _COMMIT_HEX_LENGTH and all(
        character in "0123456789abcdef" for character in line
    ):
        return line
    return None
