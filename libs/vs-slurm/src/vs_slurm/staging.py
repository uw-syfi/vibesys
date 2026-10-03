"""Content-addressed transfer cache for immutable Slurm input trees.

The transport only needs remote command execution and directory upload. A
complete object is published by atomically renaming a directory containing
both its payload and ready marker, so readers never mistake a partial upload
for a cache hit. Every call still materializes a fresh writable destination.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import stat
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence


class _ExecResponse(Protocol):
    @property
    def stdout(self) -> str: ...


class _Digest(Protocol):
    def update(self, value: bytes, /) -> None: ...


class _ContentStageTransport(Protocol):
    """Transport operations required to publish and materialize tree objects."""

    def exec(self, command: str) -> _ExecResponse:
        """Execute a safely quoted command on the remote host."""
        ...

    def sync_to(
        self,
        local: Path,
        remote: PurePosixPath,
        *,
        delete: bool,
        excludes: Sequence[str],
    ) -> None:
        """Upload one directory tree to a remote path."""
        ...


class _ContentStageError(RuntimeError):
    """Invalid or changing local input prevented safe cache publication."""

    def __init__(self, message: str, *, recoverable: bool = False) -> None:
        super().__init__(message)
        self.recoverable = recoverable

    @classmethod
    def invalid_readiness_response(cls) -> _ContentStageError:
        return cls("remote content cache returned an invalid readiness response")

    @classmethod
    def changed_during_staging(cls) -> _ContentStageError:
        return cls(
            "local input changed during remote staging; this is a transient infrastructure "
            "condition, not a candidate failure, and staging the same content again succeeds "
            "once it stops changing",
            recoverable=True,
        )

    @classmethod
    def source_is_not_directory(cls) -> _ContentStageError:
        return cls("content-addressed staging source must be a directory")

    @classmethod
    def unsupported_input(cls, relative_path: str) -> _ContentStageError:
        return cls(f"unsupported staged input type at {relative_path}")

    @classmethod
    def cache_busy(cls, digest: str) -> _ContentStageError:
        return cls(
            f"content cache object {digest} is locked by a live publisher; retry staging",
            recoverable=True,
        )


@dataclass(frozen=True)
class _ContentStageResult:
    """Identity of a materialized tree and whether its immutable object existed."""

    digest: str
    cache_hit: bool


@dataclass(frozen=True)
class _TreeStageRequest:
    source: Path
    cache_root: PurePosixPath
    destination: PurePosixPath
    trusted_parent: PurePosixPath
    staging_id: str
    excludes: tuple[str, ...] = ()


def _stage_tree(
    transport: _ContentStageTransport, request: _TreeStageRequest
) -> _ContentStageResult:
    """Reuse or publish one immutable tree, then copy it to a fresh destination."""
    digest = _tree_digest(request.source, excludes=request.excludes)
    object_root = request.cache_root / digest
    payload = object_root / "payload"
    ready_marker = object_root / "ready"
    ready = shlex.quote(ready_marker.as_posix())
    probe = transport.exec(
        f"if [ -f {ready} ]; then printf 'READY'; "
        f"elif [ -e {shlex.quote(object_root.as_posix())} ]; then printf 'INCOMPLETE'; "
        "else printf 'MISSING'; fi"
    ).stdout.strip()
    if probe not in {"READY", "MISSING"}:
        raise _ContentStageError.invalid_readiness_response()

    cache_hit = probe == "READY"
    temporary_root = request.cache_root / f"{digest}.tmp.{request.staging_id}"
    if not cache_hit:
        temporary_payload = temporary_root / "payload"
        try:
            transport.exec(f"mkdir -p {shlex.quote(temporary_payload.as_posix())}")
            transport.sync_to(
                request.source,
                temporary_payload,
                delete=True,
                excludes=request.excludes,
            )
            if _tree_digest(request.source, excludes=request.excludes) != digest:
                raise _ContentStageError.changed_during_staging()
            publish = transport.exec(
                _publish_command(temporary_root, object_root, request.staging_id)
            ).stdout.strip()
            if publish == "READY":
                cache_hit = True
            elif publish != "PUBLISHED":
                if publish == "BUSY":
                    raise _ContentStageError.cache_busy(digest)
                raise _ContentStageError.invalid_readiness_response()
        finally:
            # The name is unique to this staging request. After successful
            # rename it no longer exists, so this can never remove a cache hit.
            with suppress(Exception):
                transport.exec(f"rm -rf -- {shlex.quote(temporary_root.as_posix())}")

    transport.exec(_materialize_command(request, payload))
    return _ContentStageResult(digest=digest, cache_hit=cache_hit)


def _materialize_command(request: _TreeStageRequest, payload: PurePosixPath) -> str:
    """Replace a destination without following candidate-controlled symlinks."""
    try:
        relative = request.destination.relative_to(request.trusted_parent)
    except ValueError as exc:
        raise _ContentStageError.unsupported_input(request.destination.as_posix()) from exc
    if not relative.parts:
        raise _ContentStageError.unsupported_input(request.destination.as_posix())

    # The trusted parent is created by the caller before candidate content is
    # materialized. Every existing component below it is candidate-controlled,
    # so reject links before mkdir can traverse them. The final destination is
    # removed as a directory entry first; rm does not follow a symlink operand.
    parents = [request.trusted_parent]
    current = request.trusted_parent
    for part in relative.parts[:-1]:
        current /= part
        parents.append(current)
    link_checks = tuple(f"[ ! -L {shlex.quote(parent.as_posix())} ]" for parent in parents)
    destination = shlex.quote(request.destination.as_posix())
    return " && ".join(
        (
            *link_checks,
            f"rm -rf -- {destination}",
            f"mkdir -p -- {destination}",
            "rsync -a --chmod=Du+w,Fu+w --delete "
            f"{shlex.quote(payload.as_posix() + '/')} "
            f"{shlex.quote(request.destination.as_posix() + '/')}",
        )
    )


def _tree_digest(root: Path, *, excludes: Sequence[str] = ()) -> str:
    """Hash sorted path, type, mode, mtime, symlink target, and file contents."""
    normalized = root.resolve(strict=True)
    if not normalized.is_dir():
        raise _ContentStageError.source_is_not_directory()
    excluded_names = {PurePosixPath(item.rstrip("/")).as_posix() for item in excludes}
    digest = hashlib.sha256()
    entries = sorted(
        (
            path
            for path in normalized.rglob("*")
            if not any(
                PurePosixPath(*path.relative_to(normalized).parts[:index]).as_posix()
                in excluded_names
                for index in range(1, len(path.relative_to(normalized).parts) + 1)
            )
        ),
        key=lambda path: path.relative_to(normalized).as_posix(),
    )
    for path in entries:
        relative = path.relative_to(normalized).as_posix()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            kind = b"link"
        elif stat.S_ISDIR(metadata.st_mode):
            kind = b"dir"
        elif stat.S_ISREG(metadata.st_mode):
            kind = b"file"
        else:
            raise _ContentStageError.unsupported_input(relative)
        _update_field(digest, relative.encode("utf-8", errors="surrogateescape"))
        _update_field(digest, kind)
        _update_field(digest, str(stat.S_IMODE(metadata.st_mode)).encode("ascii"))
        _update_field(digest, str(metadata.st_mtime_ns).encode("ascii"))
        if kind == b"link":
            _update_field(digest, os.fsencode(path.readlink()))
        elif kind == b"file":
            with path.open("rb") as handle:
                while chunk := handle.read(1024 * 1024):
                    digest.update(chunk)
            _update_field(digest, b"end-file")
    return digest.hexdigest()


def tree_content_identity(root: Path, *, excludes: Sequence[str] = ()) -> str:
    """Return the canonical identity used by Slurm tree staging and its cache."""
    return _tree_digest(root, excludes=excludes)


def _publish_command(
    temporary_root: PurePosixPath, object_root: PurePosixPath, staging_id: str
) -> str:
    temporary = shlex.quote(temporary_root.as_posix())
    target = shlex.quote(object_root.as_posix())
    lock = shlex.quote((object_root.parent / f"{object_root.name}.lock").as_posix())
    owner = shlex.quote((object_root.parent / f"{object_root.name}.lock" / "owner").as_posix())
    staging = shlex.quote(staging_id)
    ready = shlex.quote((object_root / "ready").as_posix())
    stale_prefix = shlex.quote((object_root.parent / f"{object_root.name}.lock.stale").as_posix())
    temporary_ready = shlex.quote((temporary_root / "ready").as_posix())
    return (
        "for attempt in $(seq 1 30); do "
        f"if [ -f {ready} ]; then printf 'READY'; exit 0; fi; "
        f"if [ -e {target} ]; then exit 73; fi; "
        f"if mkdir -- {lock} 2>/dev/null; then "
        f"trap 'rm -f -- {owner}; rmdir -- {lock}' EXIT; "
        f'printf \'owner=%s\\npid=%s\\ncreated=%s\\n\' {staging} "$$" "$(date +%s)" > {owner}; '
        f"if [ -f {ready} ]; then printf 'READY'; exit 0; fi; "
        f"if [ -e {target} ]; then exit 73; fi; "
        f"touch {temporary_ready} && mv -T -- {temporary} {target} || exit 74; "
        "printf 'PUBLISHED'; exit 0; fi; "
        f"owner_pid=$(sed -n 's/^pid=//p' {owner} 2>/dev/null); "
        f"created_at=$(sed -n 's/^created=//p' {owner} 2>/dev/null); "
        "now=$(date +%s); "
        'if [ -n "$owner_pid" ] && [ -n "$created_at" ] && '
        '[ "$now" -ge "$created_at" ] && [ $((now - created_at)) -ge 300 ] && '
        '! kill -0 "$owner_pid" 2>/dev/null && '
        f"[ ! -e {target} ] && [ ! -f {ready} ]; then "
        f"stale={stale_prefix}.$$; "
        f'if mv -- {lock} "$stale" 2>/dev/null; then rm -rf -- "$stale"; continue; fi; fi; '
        "sleep 1; done; printf 'BUSY'; exit 0"
    )


def _update_field(digest: _Digest, value: bytes) -> None:
    digest.update(len(value).to_bytes(8, byteorder="big"))
    digest.update(value)
