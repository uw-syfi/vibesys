"""Content-addressed transfer cache for immutable Slurm input trees.

The transport only needs remote command execution and directory upload. A
complete object is published by atomically renaming a directory containing
both its payload and ready marker, so readers never mistake a partial upload
for a cache hit. Every call still materializes a fresh writable destination.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import shlex
import shutil
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, BinaryIO, Literal, Protocol

from vs_sim.api import ProcessOutcome

from .connector_protocol import MAX_TRANSPORT_STREAM_BYTES

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from vs_sim.api import Processes, ProcessRun, ProcessSpec

_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_COPY_CHUNK_BYTES = 1024 * 1024


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
    temporary_root = request.cache_root / f"{digest}.tmp.{request.staging_id}"
    temporary_payload = temporary_root / "payload"
    # One round trip decides hit or miss and, on a miss, creates the upload directory.
    probe = transport.exec(
        f"if [ -f {ready} ]; then printf 'READY'; "
        f"elif [ -e {shlex.quote(object_root.as_posix())} ]; then printf 'INCOMPLETE'; "
        f"else mkdir -p {shlex.quote(temporary_payload.as_posix())} && printf 'MISSING'; fi"
    ).stdout.strip()
    if probe not in {"READY", "MISSING"}:
        raise _ContentStageError.invalid_readiness_response()

    cache_hit = probe == "READY"
    if not cache_hit:
        published = False
        try:
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
            elif publish == "PUBLISHED":
                published = True
            elif publish == "BUSY":
                raise _ContentStageError.cache_busy(digest)
            else:
                raise _ContentStageError.invalid_readiness_response()
        finally:
            # The name is unique to this staging request, so this can never remove a
            # cache hit. A successful publish renamed it away: nothing to remove.
            if not published:
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
    nested = shlex.quote((object_root / temporary_root.name).as_posix())
    return (
        "for attempt in $(seq 1 30); do "
        f"if [ -f {ready} ]; then printf 'READY'; exit 0; fi; "
        f"if [ -e {target} ]; then exit 73; fi; "
        f"if mkdir -- {lock} 2>/dev/null; then "
        f"trap 'rm -f -- {owner}; rmdir -- {lock}' EXIT; "
        f'printf \'owner=%s\\npid=%s\\ncreated=%s\\n\' {staging} "$$" "$(date +%s)" > {owner}; '
        f"if [ -f {ready} ]; then printf 'READY'; exit 0; fi; "
        f"if [ -e {target} ]; then exit 73; fi; "
        # POSIX mv has no -T: if a directory appeared at the target after the
        # check above, mv would move the temporary tree inside it and exit 0.
        # Publishing counts only if the target is the ready tree itself.
        f"touch {temporary_ready} && mv -- {temporary} {target} && "
        f"[ -f {ready} ] && [ ! -e {nested} ] || exit 74; "
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


class _LocalTransferError(OSError):
    """A brokered local path could not be staged without crossing its authorized root."""


@dataclass(frozen=True, slots=True)
class _AuthorizedLocalPath:
    root: Path
    relative: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _LocalTransfer:
    target: _AuthorizedLocalPath
    direction: Literal["upload", "download"]
    kind: Literal["file", "tree"]
    operand_index: int = 0
    trailing_slash: bool = False


def _authorize_local_path(value: str, roots: tuple[Path, ...]) -> _AuthorizedLocalPath:
    """Resolve a canonical local spelling to one root-relative descriptor walk."""
    path = Path(value)
    if "\x00" in value or not path.is_absolute() or str(path) != value or ".." in path.parts:
        raise _LocalTransferError
    candidates: list[_AuthorizedLocalPath] = []
    for root in roots:
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        candidates.append(_AuthorizedLocalPath(root, relative.parts))
    if not candidates:
        raise _LocalTransferError
    return max(candidates, key=lambda candidate: len(candidate.root.parts))


def _run_staged_transfer(
    *,
    transfer: _LocalTransfer,
    processes: Processes,
    timeout: float,
    rewrite: Callable[[str], ProcessSpec],
) -> ProcessRun:
    """Run a transfer only against a private snapshot, then safely publish downloads."""
    try:
        temporary = tempfile.TemporaryDirectory(prefix="vibesys-slurm-broker-")
    except OSError as error:
        raise _LocalTransferError from error
    with temporary as raw:
        staged = Path(raw) / "transfer"
        try:
            try:
                if transfer.direction == "upload":
                    _copy_from_authorized(transfer.target, staged, kind=transfer.kind)
                elif transfer.kind == "tree":
                    staged.mkdir()
            except (OSError, RuntimeError, ValueError) as error:
                raise _LocalTransferError from error
            local = str(staged) + ("/" if transfer.trailing_slash else "")
            rewritten = rewrite(local)
            bounded = replace(
                rewritten,
                max_output_bytes=min(rewritten.max_output_bytes, MAX_TRANSPORT_STREAM_BYTES),
            )
            result = processes.run(bounded, timeout=timeout)
            if (
                transfer.direction == "download"
                and isinstance(result, ProcessOutcome)
                and result.returncode == 0
            ):
                try:
                    _publish_to_authorized(staged, transfer.target, kind=transfer.kind)
                except (OSError, RuntimeError, ValueError) as error:
                    raise _LocalTransferError from error
            return result
        finally:
            _make_staged_tree_removable(staged)


def _copy_from_authorized(
    source: _AuthorizedLocalPath, destination: Path, *, kind: Literal["file", "tree"]
) -> None:
    root = os.open(source.root, _DIRECTORY_FLAGS)
    try:
        if kind == "tree":
            directory = _open_directory(root, source.relative, create=False)
            try:
                destination.mkdir()
                _copy_tree_out(directory, destination)
            finally:
                os.close(directory)
        else:
            descriptor = _open_file(root, source.relative)
            try:
                _copy_descriptor_to_path(descriptor, destination)
            finally:
                os.close(descriptor)
    finally:
        os.close(root)


def _publish_to_authorized(
    source: Path, destination: _AuthorizedLocalPath, *, kind: Literal["file", "tree"]
) -> None:
    root = os.open(destination.root, _DIRECTORY_FLAGS)
    try:
        if kind == "tree":
            status = source.stat(follow_symlinks=False)
            if not stat.S_ISDIR(status.st_mode):
                raise _LocalTransferError
            target = _open_directory(root, destination.relative, create=True)
            try:
                with os.scandir(source) as entries:
                    _copy_tree_in(entries, target)
                os.fchmod(target, stat.S_IMODE(status.st_mode))
                os.utime(target, ns=(status.st_atime_ns, status.st_mtime_ns))
            finally:
                os.close(target)
        else:
            if not destination.relative:
                raise _LocalTransferError
            parent = _open_directory(root, destination.relative[:-1], create=True)
            try:
                descriptor = os.open(source, _READ_FLAGS)
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    os.close(descriptor)
                    raise _LocalTransferError
                with os.fdopen(descriptor, "rb", buffering=0) as reader:
                    _atomic_copy_in(reader, parent, destination.relative[-1])
            finally:
                os.close(parent)
    finally:
        os.close(root)


def _open_directory(root: int, parts: tuple[str, ...], *, create: bool) -> int:
    current = os.dup(root)
    try:
        for part in parts:
            if create:
                with suppress(FileExistsError):
                    os.mkdir(part, dir_fd=current)
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=current)
            os.close(current)
            current = child
    except BaseException:
        os.close(current)
        raise
    else:
        return current


def _open_file(root: int, parts: tuple[str, ...]) -> int:
    if not parts:
        raise _LocalTransferError
    parent = _open_directory(root, parts[:-1], create=False)
    try:
        descriptor = os.open(parts[-1], _READ_FLAGS, dir_fd=parent)
    finally:
        os.close(parent)
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise _LocalTransferError
    return descriptor


def _copy_tree_out(source: int, destination: Path) -> None:
    root_status = os.fstat(source)
    with os.scandir(source) as iterator:
        entries = sorted(iterator, key=lambda entry: entry.name)
    for entry in entries:
        status = os.stat(entry.name, dir_fd=source, follow_symlinks=False)
        target = destination / entry.name
        if stat.S_ISDIR(status.st_mode):
            child = os.open(entry.name, _DIRECTORY_FLAGS, dir_fd=source)
            try:
                target.mkdir()
                _copy_tree_out(child, target)
            finally:
                os.close(child)
        elif stat.S_ISREG(status.st_mode):
            descriptor = os.open(entry.name, _READ_FLAGS, dir_fd=source)
            try:
                _copy_descriptor_to_path(descriptor, target)
            finally:
                os.close(descriptor)
        elif stat.S_ISLNK(status.st_mode):
            target.symlink_to(os.readlink(entry.name, dir_fd=source))
            os.utime(
                target,
                ns=(status.st_atime_ns, status.st_mtime_ns),
                follow_symlinks=False,
            )
        else:
            raise _LocalTransferError
    destination.chmod(stat.S_IMODE(root_status.st_mode))
    os.utime(destination, ns=(root_status.st_atime_ns, root_status.st_mtime_ns))


def _copy_descriptor_to_path(source: int, destination: Path) -> None:
    status = os.fstat(source)
    if not stat.S_ISREG(status.st_mode):
        raise _LocalTransferError
    with (
        os.fdopen(os.dup(source), "rb", buffering=0) as reader,
        destination.open("xb", buffering=0) as writer,
    ):
        shutil.copyfileobj(reader, writer, _COPY_CHUNK_BYTES)
        os.fchmod(writer.fileno(), stat.S_IMODE(status.st_mode))
        os.utime(writer.fileno(), ns=(status.st_atime_ns, status.st_mtime_ns))


def _copy_tree_in(entries: Iterable[os.DirEntry[str]], destination: int) -> None:
    for entry in sorted(entries, key=lambda item: item.name):
        status = entry.stat(follow_symlinks=False)
        if stat.S_ISDIR(status.st_mode):
            with suppress(FileExistsError):
                os.mkdir(entry.name, dir_fd=destination)
            child = os.open(entry.name, _DIRECTORY_FLAGS, dir_fd=destination)
            try:
                with os.scandir(entry.path) as nested:
                    _copy_tree_in(nested, child)
                os.fchmod(child, stat.S_IMODE(status.st_mode))
                os.utime(child, ns=(status.st_atime_ns, status.st_mtime_ns))
            finally:
                os.close(child)
        elif stat.S_ISREG(status.st_mode):
            with Path(entry.path).open("rb") as reader:
                _atomic_copy_in(reader, destination, entry.name)
        elif stat.S_ISLNK(status.st_mode):
            temporary = _temporary_name(entry.name)
            try:
                os.symlink(Path(entry.path).readlink(), temporary, dir_fd=destination)
                os.utime(
                    temporary,
                    ns=(status.st_atime_ns, status.st_mtime_ns),
                    dir_fd=destination,
                    follow_symlinks=False,
                )
                os.replace(
                    temporary,
                    entry.name,
                    src_dir_fd=destination,
                    dst_dir_fd=destination,
                )
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=destination)
        else:
            raise _LocalTransferError


def _atomic_copy_in(source: BinaryIO, directory: int, name: str) -> None:
    status = os.fstat(source.fileno())
    temporary = _temporary_name(name)
    try:
        descriptor = os.open(temporary, _CREATE_FLAGS, 0o600, dir_fd=directory)
        with os.fdopen(descriptor, "wb", buffering=0) as writer:
            shutil.copyfileobj(source, writer, _COPY_CHUNK_BYTES)
            os.fchmod(writer.fileno(), stat.S_IMODE(status.st_mode))
            os.utime(writer.fileno(), ns=(status.st_atime_ns, status.st_mtime_ns))
        os.replace(temporary, name, src_dir_fd=directory, dst_dir_fd=directory)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary, dir_fd=directory)


def _temporary_name(name: str) -> str:
    return f".{name}.vibesys-{secrets.token_hex(12)}.tmp"


def _make_staged_tree_removable(path: Path) -> None:
    """Restore owner access needed for ``TemporaryDirectory`` cleanup."""
    try:
        status = path.stat(follow_symlinks=False)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(status.st_mode):
        return
    path.chmod(stat.S_IMODE(status.st_mode) | 0o700, follow_symlinks=False)
    root = os.open(path, _DIRECTORY_FLAGS)
    try:
        _make_staged_directories_removable(root)
    finally:
        os.close(root)


def _make_staged_directories_removable(directory: int) -> None:
    with os.scandir(directory) as iterator:
        entries = sorted(iterator, key=lambda entry: entry.name)
    for entry in entries:
        status = os.stat(entry.name, dir_fd=directory, follow_symlinks=False)
        if not stat.S_ISDIR(status.st_mode):
            continue
        os.chmod(
            entry.name,
            stat.S_IMODE(status.st_mode) | 0o700,
            dir_fd=directory,
            follow_symlinks=False,
        )
        child = os.open(entry.name, _DIRECTORY_FLAGS, dir_fd=directory)
        try:
            _make_staged_directories_removable(child)
        finally:
            os.close(child)
