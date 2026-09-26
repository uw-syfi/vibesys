"""Storage-neutral progress log and local/GitHub implementations."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Protocol

from vs_github.api import GitHubCLI, GitHubClient

_HEADER = "# Experiment Progress\n\n"
_LABEL = "vibesys:progress-log"
_ENTRY_START = "<!-- vibesys:progress-entry:v1\n"
_ENTRY_END = "\n-->"


class _InvalidIssuePayloadError(ValueError):
    """Raised when GitHub returns an issue snapshot without a valid number."""

    def __init__(self, value: object) -> None:
        super().__init__(f"GitHub issue number must be an integer, got {value!r}")


class ProgressLog(Protocol):
    """Storage-neutral append-only run notes."""

    def read(self) -> str:
        """Return the accumulated progress text."""
        ...

    def append(self, entry: str) -> None:
        """Append one fully formatted progress entry."""
        ...


class FileProgressLog:
    """Markdown progress log stored at one local path."""

    def __init__(self, path: Path) -> None:
        """Open a local progress log, creating its parent and header if absent."""
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.write_text(_HEADER, encoding="utf-8")

    def read(self) -> str:
        """Return the complete log as UTF-8 text."""
        return self._path.read_text(encoding="utf-8")

    def append(self, entry: str) -> None:
        """Append one Markdown entry without changing prior entries."""
        with self._path.open("a", encoding="utf-8") as stream:
            stream.write(entry)


class GitHubProgressLog:
    """Run notes stored in comments on a separate tagged GitHub issue."""

    def __init__(self, repository: str, run_id: str, *, cli: GitHubClient | None = None) -> None:
        """Bind the progress log to the GitHub issue associated with this run."""
        self._cli = cli or GitHubCLI()
        self._cli.ensure_authenticated()
        self._cli.ensure_label(repository, _LABEL)
        self._repository = repository
        self._title = f"VibeSys progress: {run_id}"
        self._issue_id = self._find_or_create()

    def read(self) -> str:
        """Return the header and all valid progress-entry comments."""
        comments = self._cli.view_issue(self._repository, self._issue_id).get("comments", [])
        if not isinstance(comments, list):
            return _HEADER
        entries = [_progress_from_comment(item) for item in comments if isinstance(item, dict)]
        return _HEADER + "".join(entry for entry in entries if entry is not None)

    def append(self, entry: str) -> None:
        """Append one progress entry as a versioned machine comment."""
        encoded = json.dumps(entry, ensure_ascii=False)
        self._cli.comment_issue(
            self._repository, self._issue_id, f"{_ENTRY_START}{encoded}{_ENTRY_END}"
        )

    def _find_or_create(self) -> int:
        for issue in self._cli.list_issues(self._repository):
            if issue.get("title") == self._title and _LABEL in _label_names(issue.get("labels")):
                return _number(issue.get("number"))
        return self._cli.create_issue(
            self._repository,
            title=self._title,
            body="VibeSys run progress log. Entries are stored as versioned comments.",
            labels=[_LABEL],
        )


def open_progress_log(
    backend: Literal["local", "github"],
    *,
    local_path: Path,
    repository: str | None = None,
    run_id: str,
) -> ProgressLog:
    """Construct the selected implementation behind the ProgressLog contract."""
    if backend == "local":
        return FileProgressLog(local_path)
    if repository is None:
        raise ValueError("repository is required for the GitHub progress backend")  # noqa: TRY003  # lint-waiver: LW-920423 [TRY003]; name the missing progress backend configuration field.
    return GitHubProgressLog(repository, run_id)


def _progress_from_comment(comment: dict[str, object]) -> str | None:
    body = str(comment.get("body", ""))
    if not body.startswith(_ENTRY_START) or not body.endswith(_ENTRY_END):
        return None
    try:
        value = json.loads(body[len(_ENTRY_START) : -len(_ENTRY_END)])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, str) else None


def _number(value: object) -> int:
    if not isinstance(value, (int, str)):
        raise _InvalidIssuePayloadError(value)
    return int(value)


def _label_names(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {
        str(label.get("name"))
        for label in value
        if isinstance(label, dict) and isinstance(label.get("name"), str)
    }


__all__ = [
    "FileProgressLog",
    "GitHubProgressLog",
    "ProgressLog",
    "open_progress_log",
]
