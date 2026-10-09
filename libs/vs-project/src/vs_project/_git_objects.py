"""Git's object model, in memory: file entries, tree and commit ids, commit records.

Blob and tree ids are Git's real ones (SHA-1 over Git's object encoding), so two
repositories holding the same files share tree ids with a real Git repository.
Commit ids hash a commit whose timestamp is the repository's commit ordinal, so
a replayed history gets the same ids, and two commits never collide.

Everything here is pure: no files, no clock, no globals.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING

from vs_project.api.git_repository import COMMIT_IDENTITY_EMAIL, COMMIT_IDENTITY_NAME

if TYPE_CHECKING:
    from collections.abc import Mapping

REGULAR = 0o100644
EXECUTABLE = 0o100755
SYMLINK = 0o120000

_TIMESTAMP_ORIGIN = 1_700_000_000
_OBJECT_PREFIX = re.compile(r"^[0-9a-f]{4,40}$")


@dataclass(frozen=True, slots=True)
class FileEntry:
    """One tracked file: its mode and the id of its content."""

    mode: int
    blob: str

    def is_symlink(self) -> bool:
        """Whether the entry is a symbolic link (its content is the link target)."""
        return self.mode == SYMLINK


type Snapshot = Mapping[str, FileEntry]
"""A flat tree: POSIX path to entry. Directories are implied by the paths."""

EMPTY_SNAPSHOT: Snapshot = MappingProxyType({})


def _object_id(kind: bytes, body: bytes) -> str:
    """Git's object id: SHA-1 over the kind, size, and body. It names content, it protects none."""
    return hashlib.sha1(kind + b" %d\0" % len(body) + body, usedforsecurity=False).hexdigest()


def blob_id(data: bytes) -> str:
    """The id Git gives a blob holding ``data``."""
    return _object_id(b"blob", data)


def tree_id(snapshot: Snapshot) -> str:
    """The id Git gives the tree whose files are ``snapshot``."""
    return _tree_node_id(_nest(snapshot))


type _Node = dict[str, FileEntry | _Node]


def _nest(snapshot: Snapshot) -> _Node:
    root: _Node = {}
    for path, entry in snapshot.items():
        *parents, name = path.split("/")
        node = root
        for part in parents:
            child = node.get(part)
            if not isinstance(child, dict):
                child = {}
                node[part] = child
            node = child
        node[name] = entry
    return root


def _tree_node_id(node: _Node) -> str:
    def sort_key(item: tuple[str, FileEntry | _Node]) -> bytes:
        name, child = item
        return name.encode() + (b"/" if isinstance(child, dict) else b"")

    body = b""
    for name, child in sorted(node.items(), key=sort_key):
        if isinstance(child, dict):
            mode, child_id = "40000", _tree_node_id(child)
        else:
            mode, child_id = f"{child.mode:o}", child.blob
        body += f"{mode} {name}".encode() + b"\0" + bytes.fromhex(child_id)
    return _object_id(b"tree", body)


@dataclass(frozen=True, slots=True)
class CommitRecord:
    """One commit: its tree, parents, cleaned message, and creation ordinal."""

    id: str
    tree: str
    parents: tuple[str, ...]
    message: str
    ordinal: int

    @property
    def subject(self) -> str:
        """Git's ``%s``: the first paragraph of the message, joined into one line."""
        lines: list[str] = []
        for line in self.message.split("\n"):
            if not line:
                break
            lines.append(line)
        return " ".join(lines)


def clean_message(message: str) -> str:
    """Git's default message cleanup: trim line ends, collapse blank runs, end with a newline."""
    kept: list[str] = []
    for raw in message.splitlines():
        line = raw.rstrip()
        if not line and (not kept or not kept[-1]):
            continue
        kept.append(line)
    while kept and not kept[-1]:
        kept.pop()
    return "\n".join(kept) + "\n" if kept else ""


def _commit_id(tree: str, parents: tuple[str, ...], message: str, ordinal: int) -> str:
    stamp = f"{COMMIT_IDENTITY_NAME} <{COMMIT_IDENTITY_EMAIL}> {_TIMESTAMP_ORIGIN + ordinal} +0000"
    header = "".join(
        [
            f"tree {tree}\n",
            *(f"parent {parent}\n" for parent in parents),
            f"author {stamp}\n",
            f"committer {stamp}\n\n",
        ]
    )
    body = (header + message).encode()
    return _object_id(b"commit", body)


class ObjectStore:
    """The blobs, trees, and commits of one repository. Objects are never removed."""

    def __init__(self) -> None:
        """Start empty."""
        self._blobs: dict[str, bytes] = {}
        self._trees: dict[str, Snapshot] = {}
        self._commits: dict[str, CommitRecord] = {}

    def add_blob(self, data: bytes) -> str:
        """Store ``data`` and return its id."""
        identifier = blob_id(data)
        self._blobs.setdefault(identifier, data)
        return identifier

    def blob(self, identifier: str) -> bytes | None:
        """The bytes of a stored blob."""
        return self._blobs.get(identifier)

    def add_commit(
        self, snapshot: Snapshot, parents: tuple[str, ...], message: str
    ) -> CommitRecord:
        """Record a commit of ``snapshot`` on ``parents``; ``message`` is already clean."""
        tree = tree_id(snapshot)
        self._trees.setdefault(tree, MappingProxyType(dict(snapshot)))
        ordinal = len(self._commits) + 1
        record = CommitRecord(
            id=_commit_id(tree, parents, message, ordinal),
            tree=tree,
            parents=parents,
            message=message,
            ordinal=ordinal,
        )
        self._commits[record.id] = record
        return record

    def commit(self, identifier: str) -> CommitRecord | None:
        """The commit with this full id."""
        return self._commits.get(identifier)

    def snapshot(self, commit: CommitRecord) -> Snapshot:
        """The files of a commit's tree."""
        return self._trees[commit.tree]

    def snapshot_of(self, identifier: str) -> Snapshot:
        """The files of the commit with this id, which must exist (``KeyError`` otherwise)."""
        return self._trees[self._commits[identifier].tree]

    def has_object(self, identifier: str) -> bool:
        """Whether any object with this full id exists."""
        return identifier in self._commits or identifier in self._blobs or identifier in self._trees

    def commits_named(self, prefix: str) -> list[str]:
        """Every commit id that starts with the abbreviation ``prefix`` (empty if malformed)."""
        if _OBJECT_PREFIX.match(prefix) is None:
            return []
        return [identifier for identifier in self._commits if identifier.startswith(prefix)]

    def ancestry(self, tip: str) -> list[CommitRecord]:
        """Every commit reachable from ``tip``, newest first (by creation ordinal)."""
        seen: dict[str, CommitRecord] = {}
        pending = [tip]
        while pending:
            record = self._commits[pending.pop()]
            if record.id in seen:
                continue
            seen[record.id] = record
            pending.extend(record.parents)
        return sorted(seen.values(), key=lambda record: record.ordinal, reverse=True)

    def reaches(self, tip: str, target: str) -> bool:
        """Whether ``target`` is ``tip`` or one of its ancestors."""
        pending, seen = [tip], set[str]()
        while pending:
            current = pending.pop()
            if current == target:
                return True
            if current in seen:
                continue
            seen.add(current)
            pending.extend(self._commits[current].parents)
        return False
