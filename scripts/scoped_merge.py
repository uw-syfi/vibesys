"""Authorize and execute a path-scoped pull request merge."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Never, Protocol, cast
from urllib.parse import quote, urlencode

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = REPO_ROOT / ".github" / "scoped-merge.toml"
MAX_CHANGED_FILES = 3_000
API_TIMEOUT_SECONDS = 30
Runner = Callable[..., subprocess.CompletedProcess[str]]


class MergeRefusalError(RuntimeError):
    """An expected, user-actionable reason not to merge."""


class GitHubAPIError(RuntimeError):
    """A sanitized GitHub API failure."""

    @classmethod
    def unavailable(cls) -> GitHubAPIError:
        """Build an error for a missing GitHub CLI."""
        return cls("GitHub CLI is unavailable")

    @classmethod
    def request_failed(cls, operation: str) -> GitHubAPIError:
        """Build an error for a failed API operation."""
        return cls(f"GitHub API could not {operation}")

    @classmethod
    def malformed_json(cls) -> GitHubAPIError:
        """Build an error for a malformed API response."""
        return cls("GitHub API returned malformed JSON")


class GitHubClient(Protocol):
    """API operations required by the merge policy."""

    def get(self, endpoint: str, *, paginate: bool = False) -> object:
        """Read one API endpoint."""

    def write(self, endpoint: str, *, method: str, payload: Mapping[str, object]) -> object:
        """Write one API endpoint."""


@dataclass(frozen=True)
class Policy:
    """Trusted, versioned limits for delegated merges."""

    command: str
    repository: str
    base_branch: str
    workflow_file: str
    merge_method: str
    allowed_prefixes: tuple[str, ...]
    allowed_paths: frozenset[str]


@dataclass(frozen=True)
class Event:
    """Relevant fields from an issue-comment event."""

    repository: str
    number: int
    actor: str
    actor_type: str
    action: str
    body: str
    is_pull_request: bool


@dataclass(frozen=True)
class GitHubAPI:
    """Small, injectable adapter for authenticated ``gh api`` calls."""

    _runner: Runner = field(default=subprocess.run, repr=False, compare=False)

    def get(self, endpoint: str, *, paginate: bool = False) -> object:
        """Read and decode one endpoint."""
        arguments = ["gh", "api"]
        if paginate:
            arguments.extend(["--paginate", "--slurp"])
        arguments.append(endpoint)
        return self._run_json(arguments, operation="read GitHub state")

    def write(self, endpoint: str, *, method: str, payload: Mapping[str, object]) -> object:
        """Send one JSON request and decode its response."""
        return self._run_json(
            ["gh", "api", "--method", method, "--input", "-", endpoint],
            operation="update GitHub state",
            input=json.dumps(payload),
        )

    def _run_json(self, command: Sequence[str], *, operation: str, **kwargs: object) -> object:
        try:
            result = self._runner(
                command,
                capture_output=True,
                text=True,
                timeout=API_TIMEOUT_SECONDS,
                **kwargs,
            )
        except FileNotFoundError as exc:
            raise GitHubAPIError.unavailable() from exc
        except subprocess.TimeoutExpired as exc:
            raise GitHubAPIError.request_failed(operation) from exc
        if result.returncode != 0:
            raise GitHubAPIError.request_failed(operation)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubAPIError.malformed_json() from exc


def load_policy(path: Path = POLICY_PATH) -> Policy:
    """Load the strict merge policy from TOML."""
    document = tomllib.loads(path.read_text())
    expected = {
        "command",
        "repository",
        "base_branch",
        "workflow_file",
        "merge_method",
        "allowed_prefixes",
        "allowed_paths",
    }
    if set(document) != expected:
        _refuse(f"policy keys must be exactly {sorted(expected)}")
    strings = ("command", "repository", "base_branch", "workflow_file", "merge_method")
    if any(not isinstance(document[key], str) or not document[key] for key in strings):
        _refuse("policy scalar values must be non-empty strings")
    prefixes = _string_collection(document["allowed_prefixes"], name="allowed_prefixes")
    paths = _string_collection(document["allowed_paths"], name="allowed_paths")
    if not prefixes and not paths:
        _refuse("policy must allow at least one path")
    if any(not prefix.endswith("/") or not _safe_path(prefix[:-1]) for prefix in prefixes):
        _refuse("allowed prefixes must be safe repository paths ending in '/' ")
    if any(not _safe_path(path_value) for path_value in paths):
        _refuse("allowed paths must be safe repository paths")
    return Policy(
        command=cast("str", document["command"]),
        repository=cast("str", document["repository"]),
        base_branch=cast("str", document["base_branch"]),
        workflow_file=cast("str", document["workflow_file"]),
        merge_method=cast("str", document["merge_method"]),
        allowed_prefixes=prefixes,
        allowed_paths=frozenset(paths),
    )


def parse_event(document: object) -> Event:
    """Validate the fields consumed from the webhook payload."""
    root = _mapping(document, "event")
    repository = _mapping(root.get("repository"), "event.repository")
    issue = _mapping(root.get("issue"), "event.issue")
    comment = _mapping(root.get("comment"), "event.comment")
    actor = _mapping(comment.get("user"), "event.comment.user")
    return Event(
        repository=_string(repository.get("full_name"), "event.repository.full_name"),
        number=_positive_int(issue.get("number"), "event.issue.number"),
        actor=_string(actor.get("login"), "event.comment.user.login"),
        actor_type=_string(actor.get("type"), "event.comment.user.type"),
        action=_string(root.get("action"), "event.action"),
        body=_string(comment.get("body"), "event.comment.body"),
        is_pull_request=isinstance(issue.get("pull_request"), dict),
    )


def authorize_event(event: Event, policy: Policy, authorized_users: str) -> None:
    """Authorize the command issuer and immutable event scope."""
    users = {login.casefold() for login in re.split(r"[\s,]+", authorized_users) if login}
    if not users:
        _refuse("no scoped merge maintainers are configured")
    if event.action != "created" or not event.is_pull_request:
        _refuse("the command must be a newly created pull request comment")
    if event.repository != policy.repository:
        _refuse(f"the command is not for {policy.repository}")
    if event.actor_type != "User" or event.actor.casefold() not in users:
        _refuse(f"@{event.actor} is not an authorized scoped merge maintainer")
    if event.body.strip() != policy.command:
        _refuse(f"the exact command is {policy.command}")


def authorize_repository_access(permission: object, *, actor: str) -> None:
    """Require the command issuer to retain live repository access."""
    document = _mapping(permission, "repository permission")
    # GitHub's legacy ``permission`` field collapses Triage into ``read`` and
    # Maintain into ``write``. ``role_name`` preserves the actual role.
    if document.get("role_name") not in {"triage", "write", "maintain", "admin"}:
        _refuse(f"@{actor} no longer has Triage or higher repository access")


def authorize_pull_request(
    pull_request: object,
    *,
    policy: Policy,
    expected_number: int,
) -> str:
    """Return the head SHA after validating PR state and destination."""
    pull = _mapping(pull_request, "pull request")
    base = _mapping(pull.get("base"), "pull request base")
    base_repo = _mapping(base.get("repo"), "pull request base repository")
    head = _mapping(pull.get("head"), "pull request head")
    if pull.get("number") != expected_number:
        _refuse("GitHub returned a different pull request")
    if pull.get("state") != "open" or pull.get("merged") is True:
        _refuse("the pull request is not open")
    if pull.get("draft") is not False:
        _refuse("the pull request is a draft or its draft state is unknown")
    if base_repo.get("full_name") != policy.repository or base.get("ref") != policy.base_branch:
        _refuse(f"the pull request must target {policy.repository}:{policy.base_branch}")
    if pull.get("mergeable") is not True or pull.get("mergeable_state") != "clean":
        _refuse("the pull request is not currently clean and mergeable")
    return _string(head.get("sha"), "pull request head SHA")


def authorize_files(files: object, *, changed_files: object, policy: Policy) -> None:
    """Require a complete, non-empty diff wholly inside the delegated paths."""
    count = _positive_int(changed_files, "pull request changed_files")
    if count > MAX_CHANGED_FILES:
        _refuse(f"the pull request exceeds the {MAX_CHANGED_FILES}-file validation limit")
    pages = _list(files, "changed file pages")
    entries = [entry for page in pages for entry in _list(page, "changed file page")]
    if len(entries) != count:
        _refuse(f"GitHub returned {len(entries)} of {count} changed files")
    for entry_value in entries:
        entry = _mapping(entry_value, "changed file")
        names = [_string(entry.get("filename"), "changed file filename")]
        previous = entry.get("previous_filename")
        if previous is not None:
            names.append(_string(previous, "changed file previous_filename"))
        for name in names:
            if not _path_allowed(name, policy):
                _refuse(f"{name} is outside the delegated TUI and server paths")


def authorize_workflow(runs: object, *, head_sha: str) -> None:
    """Require the latest full test workflow run for this head to have succeeded."""
    document = _mapping(runs, "workflow runs")
    candidates = [
        _mapping(run, "workflow run")
        for run in _list(document.get("workflow_runs"), "workflow_runs")
        if isinstance(run, dict)
        and run.get("head_sha") == head_sha
        and run.get("event") == "pull_request"
    ]
    if not candidates:
        _refuse("the current pull request head has no test workflow run")
    latest = max(candidates, key=lambda run: _positive_int(run.get("id"), "workflow run id"))
    if latest.get("status") != "completed" or latest.get("conclusion") != "success":
        _refuse("the latest test workflow for the current pull request head has not succeeded")


def run(event_path: Path, *, authorized_users: str, api: GitHubClient) -> str:
    """Validate every gate, merge atomically, and return the merge SHA."""
    policy = load_policy()
    event = parse_event(json.loads(event_path.read_text()))
    authorize_event(event, policy, authorized_users)
    repository = quote(policy.repository, safe="/")
    pull_endpoint = f"repos/{repository}/pulls/{event.number}"
    actor = quote(event.actor, safe="")
    authorize_repository_access(
        api.get(f"repos/{repository}/collaborators/{actor}/permission"),
        actor=event.actor,
    )
    pull = api.get(pull_endpoint)
    head_sha = authorize_pull_request(pull, policy=policy, expected_number=event.number)
    changed_files = _mapping(pull, "pull request").get("changed_files")
    authorize_files(
        api.get(f"{pull_endpoint}/files?per_page=100", paginate=True),
        changed_files=changed_files,
        policy=policy,
    )
    query = urlencode({"head_sha": head_sha, "event": "pull_request", "per_page": 100})
    workflow = quote(policy.workflow_file, safe="")
    authorize_workflow(
        api.get(f"repos/{repository}/actions/workflows/{workflow}/runs?{query}"),
        head_sha=head_sha,
    )
    refreshed = api.get(pull_endpoint)
    refreshed_sha = authorize_pull_request(refreshed, policy=policy, expected_number=event.number)
    if refreshed_sha != head_sha:
        _refuse("the pull request changed during validation; rerun the command")
    response = _mapping(
        api.write(
            f"{pull_endpoint}/merge",
            method="PUT",
            payload={"sha": head_sha, "merge_method": policy.merge_method},
        ),
        "merge response",
    )
    if response.get("merged") is not True:
        _refuse("GitHub refused the merge")
    return _string(response.get("sha"), "merge response SHA")


def _comment(api: GitHubClient, event: Event, body: str) -> None:
    repository = quote(event.repository, safe="/")
    api.write(
        f"repos/{repository}/issues/{event.number}/comments",
        method="POST",
        payload={"body": body},
    )


def _path_allowed(path: str, policy: Policy) -> bool:
    return _safe_path(path) and (
        path in policy.allowed_paths
        or any(path.startswith(prefix) for prefix in policy.allowed_prefixes)
    )


def _safe_path(path: str) -> bool:
    parts = path.split("/")
    return (
        bool(path)
        and not path.startswith("/")
        and "\\" not in path
        and all(part not in {"", ".", ".."} for part in parts)
    )


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        _refuse(f"{name} is not an object")
    return cast("Mapping[str, object]", value)


def _list(value: object, name: str) -> list[object]:
    if not isinstance(value, list):
        _refuse(f"{name} is not a list")
    return value


def _string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        _refuse(f"{name} is not a non-empty string")
    return value


def _positive_int(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        _refuse(f"{name} is not a positive integer")
    return value


def _string_collection(value: object, *, name: str) -> tuple[str, ...]:
    values = _list(value, name)
    if not all(isinstance(item, str) and item for item in values):
        _refuse(f"{name} must contain only non-empty strings")
    strings = cast("list[str]", values)
    if len(strings) != len(set(strings)):
        _refuse(f"{name} contains duplicates")
    return tuple(strings)


def _refuse(message: str) -> Never:
    raise MergeRefusalError(message)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--event", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    """Run the merge command and leave one audit comment."""
    args = _parse_args()
    api = GitHubAPI()
    event: Event | None = None
    try:
        event = parse_event(json.loads(args.event.read_text()))
        merge_sha = run(
            args.event,
            authorized_users=os.environ.get("SCOPED_MERGE_USERS", ""),
            api=api,
        )
    except MergeRefusalError as exc:
        message = f"Scoped merge refused: {exc}."
    except (GitHubAPIError, OSError, json.JSONDecodeError, tomllib.TOMLDecodeError):
        message = "Scoped merge refused: validation could not be completed safely."
    else:
        message = f"Scoped merge completed at `{merge_sha}`."
        with suppress(GitHubAPIError):
            _comment(api, event, message)
        print(message)
        return 0
    if event is not None:
        with suppress(GitHubAPIError):
            _comment(api, event, message)
    print(message, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
