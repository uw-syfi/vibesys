"""Authorize and execute a capability-scoped pull request merge."""

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
POLICY_PATH = REPO_ROOT / ".github" / "delegated-merge.toml"
MAX_CHANGED_FILES = 3_000
API_TIMEOUT_SECONDS = 30
HARD_DENIED_PATHS = frozenset(
    {
        ".github/delegated-merge.toml",
        ".github/workflows/delegated-merge.yml",
        "scripts/delegated_merge.py",
    }
)
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
class Check:
    """One required Actions job and its owning workflow."""

    workflow_file: str
    job_name: str


@dataclass(frozen=True)
class Rule:
    """Capabilities and extra checks required by a set of repository paths."""

    prefixes: tuple[str, ...]
    paths: frozenset[str]
    requires: frozenset[str]
    additional_checks: frozenset[str]


@dataclass(frozen=True)
class Policy:
    """Trusted, versioned limits for delegated merges."""

    schema_version: int
    command: str
    repository: str
    base_branch: str
    merge_method: str
    required_checks: frozenset[str]
    checks: Mapping[str, Check]
    rules: tuple[Rule, ...]


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
        "schema_version",
        "command",
        "repository",
        "base_branch",
        "merge_method",
        "required_checks",
        "checks",
        "rules",
    }
    if set(document) != expected:
        _refuse(f"policy keys must be exactly {sorted(expected)}")
    if document["schema_version"] != 1:
        _refuse("policy schema_version must be 1")
    strings = ("command", "repository", "base_branch", "merge_method")
    if any(not isinstance(document[key], str) or not document[key] for key in strings):
        _refuse("policy scalar values must be non-empty strings")
    if not cast("str", document["command"]).startswith("/merge-"):
        _refuse("policy command must start with '/merge-'")
    checks = _load_checks(document["checks"])
    required_checks = frozenset(
        _string_collection(document["required_checks"], name="required_checks")
    )
    if not required_checks:
        _refuse("policy must define at least one required check")
    rules = _load_rules(document["rules"])
    referenced_checks = required_checks | frozenset(
        check_id for rule in rules for check_id in rule.additional_checks
    )
    unknown_checks = referenced_checks - checks.keys()
    if unknown_checks:
        _refuse(f"policy references undefined checks: {sorted(unknown_checks)}")
    return Policy(
        schema_version=1,
        command=cast("str", document["command"]),
        repository=cast("str", document["repository"]),
        base_branch=cast("str", document["base_branch"]),
        merge_method=cast("str", document["merge_method"]),
        required_checks=required_checks,
        checks=checks,
        rules=rules,
    )


def _load_checks(raw_checks: object) -> Mapping[str, Check]:
    checks_document = _mapping(raw_checks, "checks")
    checks: dict[str, Check] = {}
    for check_id, check_value in checks_document.items():
        if not _identifier(check_id):
            _refuse(f"check ID {check_id!r} is invalid")
        check_document = _mapping(check_value, f"checks.{check_id}")
        if set(check_document) != {"workflow_file", "job_name"}:
            _refuse(f"checks.{check_id} must define workflow_file and job_name")
        checks[check_id] = Check(
            workflow_file=_string(
                check_document["workflow_file"], f"checks.{check_id}.workflow_file"
            ),
            job_name=_string(check_document["job_name"], f"checks.{check_id}.job_name"),
        )
        if not _safe_path(checks[check_id].workflow_file) or "/" in checks[check_id].workflow_file:
            _refuse(f"checks.{check_id}.workflow_file must be a workflow filename")
    return checks


def _load_rules(raw_rules: object) -> tuple[Rule, ...]:
    rules_document = _list(raw_rules, "rules")
    if not rules_document:
        _refuse("policy must define at least one rule")
    rules: list[Rule] = []
    for index, rule_value in enumerate(rules_document):
        rule_document = _mapping(rule_value, f"rules[{index}]")
        expected_rule = {"prefixes", "paths", "requires", "additional_checks"}
        if set(rule_document) != expected_rule:
            _refuse(f"rules[{index}] keys must be exactly {sorted(expected_rule)}")
        prefixes = _string_collection(rule_document["prefixes"], name=f"rules[{index}].prefixes")
        paths = _string_collection(rule_document["paths"], name=f"rules[{index}].paths")
        requires = frozenset(
            _string_collection(rule_document["requires"], name=f"rules[{index}].requires")
        )
        additional_checks = frozenset(
            _string_collection(
                rule_document["additional_checks"], name=f"rules[{index}].additional_checks"
            )
        )
        if not prefixes and not paths:
            _refuse(f"rules[{index}] must match at least one path")
        if not requires:
            _refuse(f"rules[{index}] must require at least one capability")
        if any(not prefix.endswith("/") or not _safe_path(prefix[:-1]) for prefix in prefixes):
            _refuse(f"rules[{index}] prefixes must be safe repository paths ending in '/'")
        if any(not _safe_path(path_value) for path_value in paths):
            _refuse(f"rules[{index}] paths must be safe repository paths")
        if any(not _identifier(capability) for capability in requires):
            _refuse(f"rules[{index}] contains an invalid capability")
        rules.append(
            Rule(
                prefixes=prefixes,
                paths=frozenset(paths),
                requires=requires,
                additional_checks=additional_checks,
            )
        )
    return tuple(rules)


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


def authorize_event(event: Event, policy: Policy) -> None:
    """Authorize the immutable event scope and exact command."""
    if event.action != "created" or not event.is_pull_request:
        _refuse("the command must be a newly created pull request comment")
    if event.repository != policy.repository:
        _refuse(f"the command is not for {policy.repository}")
    if event.actor_type != "User":
        _refuse("the command issuer must be a GitHub user")
    if event.body != policy.command:
        _refuse(f"the exact command is {policy.command}")


def load_grants(document: str) -> Mapping[str, frozenset[str]]:
    """Parse the case-insensitive login-to-capabilities grant map."""
    try:
        value = json.loads(document)
    except json.JSONDecodeError:
        _refuse("DELEGATED_MERGE_GRANTS is not valid JSON")
    grants_document = _mapping(value, "DELEGATED_MERGE_GRANTS")
    grants: dict[str, frozenset[str]] = {}
    for login, capabilities_value in grants_document.items():
        normalized = login.casefold()
        if not login or not normalized or normalized in grants:
            _refuse("DELEGATED_MERGE_GRANTS contains an empty or duplicate login")
        capabilities = frozenset(_string_collection(capabilities_value, name=f"grants.{login}"))
        if not capabilities or any(
            capability != "*" and not _identifier(capability) for capability in capabilities
        ):
            _refuse(f"grants.{login} must contain valid capabilities or '*'")
        grants[normalized] = capabilities
    return grants


def authorize_capabilities(
    grants: Mapping[str, frozenset[str]], *, actor: str, required: frozenset[str]
) -> None:
    """Require the command issuer to hold every capability selected by the diff."""
    held = grants.get(actor.casefold())
    if held is None:
        _refuse(f"@{actor} has no delegated merge grant")
    missing = required - held
    if "*" not in held and missing:
        _refuse(f"@{actor} lacks required capabilities: {sorted(missing)}")


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


def authorize_files(
    files: object, *, changed_files: object, policy: Policy
) -> tuple[frozenset[str], frozenset[str]]:
    """Resolve the capability and check union for a complete changed-file list."""
    count = _positive_int(changed_files, "pull request changed_files")
    if count > MAX_CHANGED_FILES:
        _refuse(f"the pull request exceeds the {MAX_CHANGED_FILES}-file validation limit")
    pages = _list(files, "changed file pages")
    entries = [entry for page in pages for entry in _list(page, "changed file page")]
    if len(entries) != count:
        _refuse(f"GitHub returned {len(entries)} of {count} changed files")
    required_capabilities: set[str] = set()
    required_checks = set(policy.required_checks)
    hard_denied = HARD_DENIED_PATHS | {
        f".github/workflows/{check.workflow_file}" for check in policy.checks.values()
    }
    for entry_value in entries:
        entry = _mapping(entry_value, "changed file")
        names = [_string(entry.get("filename"), "changed file filename")]
        previous = entry.get("previous_filename")
        if previous is not None:
            names.append(_string(previous, "changed file previous_filename"))
        for name in names:
            if name in hard_denied:
                _refuse(f"{name} is controlled by the delegated merge broker")
            if not _safe_path(name):
                _refuse(f"{name} is not a safe repository path")
            matching = [rule for rule in policy.rules if _rule_matches(name, rule)]
            if not matching:
                _refuse(f"{name} does not match a delegated merge rule")
            required_capabilities.update(
                capability for rule in matching for capability in rule.requires
            )
            required_checks.update(
                check_id for rule in matching for check_id in rule.additional_checks
            )
    return frozenset(required_capabilities), frozenset(required_checks)


def select_workflow_run(runs: object, *, head_sha: str, check_id: str) -> int:
    """Select the latest pull-request workflow run for an exact head SHA."""
    document = _mapping(runs, "workflow runs")
    candidates = [
        _mapping(run, "workflow run")
        for run in _list(document.get("workflow_runs"), "workflow_runs")
        if isinstance(run, dict)
        and run.get("head_sha") == head_sha
        and run.get("event") == "pull_request"
    ]
    if not candidates:
        _refuse(f"check {check_id!r} has no workflow run for the current pull request head")
    latest = max(candidates, key=lambda run: _positive_int(run.get("id"), "workflow run id"))
    return _positive_int(latest.get("id"), "workflow run id")


def authorize_check_job(jobs: object, *, check_id: str, job_name: str) -> None:
    """Require one exact, unambiguous successful job in the selected workflow run."""
    pages = _list(jobs, "workflow job pages")
    entries: list[Mapping[str, object]] = []
    for page in pages:
        page_document = _mapping(page, "workflow job page")
        entries.extend(
            _mapping(job, "workflow job")
            for job in _list(page_document.get("jobs"), "workflow jobs")
        )
    matches = [job for job in entries if job.get("name") == job_name]
    if len(matches) != 1:
        _refuse(
            f"check {check_id!r} expected exactly one job named {job_name!r}, found {len(matches)}"
        )
    job = matches[0]
    if job.get("status") != "completed" or job.get("conclusion") != "success":
        _refuse(f"check {check_id!r} job {job_name!r} has not succeeded")


def run(event_path: Path, *, grants_json: str, api: GitHubClient) -> str:
    """Validate every gate, merge atomically, and return the merge SHA."""
    policy = load_policy()
    event = parse_event(json.loads(event_path.read_text()))
    authorize_event(event, policy)
    grants = load_grants(grants_json)
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
    required_capabilities, required_checks = authorize_files(
        api.get(f"{pull_endpoint}/files?per_page=100", paginate=True),
        changed_files=changed_files,
        policy=policy,
    )
    authorize_capabilities(grants, actor=event.actor, required=required_capabilities)
    query = urlencode({"head_sha": head_sha, "event": "pull_request", "per_page": 100})
    for check_id in sorted(required_checks):
        check = policy.checks[check_id]
        workflow = quote(check.workflow_file, safe="")
        run_id = select_workflow_run(
            api.get(f"repos/{repository}/actions/workflows/{workflow}/runs?{query}"),
            head_sha=head_sha,
            check_id=check_id,
        )
        authorize_check_job(
            api.get(f"repos/{repository}/actions/runs/{run_id}/jobs?per_page=100", paginate=True),
            check_id=check_id,
            job_name=check.job_name,
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


def _rule_matches(path: str, rule: Rule) -> bool:
    return path in rule.paths or any(path.startswith(prefix) for prefix in rule.prefixes)


def _identifier(value: str) -> bool:
    return re.fullmatch(r"[a-z][a-z0-9_-]*", value) is not None


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
        if event.body.strip() != load_policy().command:
            return 0
        merge_sha = run(
            args.event,
            grants_json=os.environ.get("DELEGATED_MERGE_GRANTS", ""),
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
