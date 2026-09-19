"""Authorize and execute a capability-scoped pull request merge."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Never, Protocol, cast
from urllib.parse import quote, urlencode

REPO_ROOT = Path(__file__).resolve().parents[1]
POLICY_PATH = REPO_ROOT / ".github" / "delegated-merge.toml"
MAX_CHANGED_FILES = 3_000
MAX_GITHUB_LOGIN_LENGTH = 39
MAX_GRAPHQL_ERROR_TYPES = 5
API_TIMEOUT_SECONDS = 30
LANDING_TOKEN_ENV = "LANDING_GH_TOKEN"  # noqa: S105  # env var name, not a secret
HARD_DENIED_PATHS = frozenset(
    {
        ".github/delegated-merge.toml",
        ".github/workflows/delegated-merge.yml",
        "scripts/delegated_merge.py",
    }
)
Runner = Callable[..., subprocess.CompletedProcess[str]]
RepositoryRole = Literal["triage", "write", "maintain", "admin"]


class MergeRefusalError(RuntimeError):
    """An expected, user-actionable reason not to merge."""


class GitHubAPIError(RuntimeError):
    """A sanitized GitHub API failure.

    ``step`` names the policy step that made the call and ``detail`` holds only
    allow-listed facts (HTTP status code, GraphQL error type names). Neither
    ever carries tokens, response bodies, or GitHub-provided free text.
    """

    def __init__(self, message: str, *, detail: str = "") -> None:
        """Record the sanitized message and allow-listed detail."""
        super().__init__(message)
        self.detail = detail
        self.step = ""

    @classmethod
    def unavailable(cls) -> GitHubAPIError:
        """Build an error for a missing GitHub CLI."""
        return cls("GitHub CLI is unavailable", detail="gh not found")

    @classmethod
    def request_failed(cls, operation: str, *, detail: str = "") -> GitHubAPIError:
        """Build an error for a failed API operation."""
        return cls(f"GitHub API could not {operation}", detail=detail)

    @classmethod
    def graphql_failed(cls, *, detail: str = "") -> GitHubAPIError:
        """Build an error for a GraphQL operation that reported errors."""
        return cls("GitHub GraphQL reported an error", detail=detail)

    @classmethod
    def malformed_json(cls) -> GitHubAPIError:
        """Build an error for a malformed API response."""
        return cls("GitHub API returned malformed JSON", detail="malformed JSON")


class GitHubClient(Protocol):
    """API operations required by the merge policy."""

    def get(self, endpoint: str, *, paginate: bool = False) -> object:
        """Read one API endpoint."""

    def write(self, endpoint: str, *, method: str, payload: Mapping[str, object]) -> object:
        """Write one API endpoint."""

    def graphql(self, query: str, variables: Mapping[str, object]) -> object:
        """Run one GraphQL operation and return its ``data`` object."""


@dataclass(frozen=True)
class Merged:
    """The pull request was merged directly; ``sha`` is the real merge commit."""

    sha: str


@dataclass(frozen=True)
class Enqueued:
    """The pull request was handed to the merge queue. Nothing is merged yet."""


@dataclass(frozen=True)
class AlreadyQueued:
    """The pull request was already in the merge queue; nothing was changed."""


LandOutcome = Merged | Enqueued | AlreadyQueued


@dataclass(frozen=True)
class LandingRequest:
    """A pull request that already passed every scope and policy check."""

    repository: str
    number: int
    node_id: str
    head_sha: str
    base_branch: str
    merge_method: str


class LandingStrategy(Protocol):
    """Lands a validated pull request, or raises ``MergeRefusalError``."""

    def land(self, request: LandingRequest) -> LandOutcome:
        """Merge or enqueue the request."""


@dataclass(frozen=True)
class QueueState:
    """Merge queue facts for one pull request and its base branch."""

    pull_request_id: str
    queue_required: bool
    in_queue: bool


@dataclass(frozen=True)
class Check:
    """One required Actions job and its owning workflow."""

    workflow_file: str
    job_name: str


@dataclass(frozen=True)
class Capability:
    """Repository paths and extra checks governed by one named capability."""

    members: frozenset[str]
    prefixes: tuple[str, ...]
    paths: frozenset[str]
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
    capabilities: Mapping[str, Capability]


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
    _token: str | None = field(default=None, repr=False, compare=False)

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

    def graphql(self, query: str, variables: Mapping[str, object]) -> object:
        """Run one GraphQL operation; any reported error fails the call."""
        document = self._run_json(
            ["gh", "api", "graphql", "--input", "-"],
            operation="run GitHub GraphQL",
            input=json.dumps({"query": query, "variables": variables}),
        )
        root = _mapping(document, "GraphQL response")
        if root.get("errors"):
            raise GitHubAPIError.graphql_failed(detail=_graphql_error_detail(root))
        return root.get("data")

    def _run_json(self, command: Sequence[str], *, operation: str, **kwargs: object) -> object:
        if self._token is not None:
            kwargs["env"] = {**os.environ, "GH_TOKEN": self._token}
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
            raise GitHubAPIError.request_failed(operation, detail="timeout") from exc
        if result.returncode != 0:
            raise GitHubAPIError.request_failed(operation, detail=_failure_detail(result))
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise GitHubAPIError.malformed_json() from exc


def landing_client(environ: Mapping[str, str] = os.environ) -> GitHubAPI | None:
    """Build the write client for landing from ``LANDING_GH_TOKEN``, or None if unset.

    The landing token is a GitHub App installation token: pull requests
    enqueued with ``GITHUB_TOKEN`` do not start the merge queue's CI. It is
    never derived from, or replaced by, the read token.
    """
    token = environ.get(LANDING_TOKEN_ENV, "").strip()
    return GitHubAPI(_token=token) if token else None


def _graphql_error_detail(document: object) -> str:
    """Return only allow-listed GraphQL error type names, never messages."""
    if not isinstance(document, dict):
        return ""
    errors = document.get("errors")
    if not isinstance(errors, list):
        return ""
    types: list[str] = []
    for error in errors:
        error_type = error.get("type") if isinstance(error, dict) else None
        if (
            isinstance(error_type, str)
            and re.fullmatch(r"[A-Z][A-Z_]{0,39}", error_type)
            and error_type not in types
        ):
            types.append(error_type)
    return "GraphQL " + ", ".join(types[:MAX_GRAPHQL_ERROR_TYPES]) if types else ""


def _failure_detail(result: subprocess.CompletedProcess[str]) -> str:
    """Summarize a failed ``gh api`` call as an HTTP status and GraphQL error types."""
    parts: list[str] = []
    status = re.search(r"\(HTTP (\d{3})\)", result.stderr or "")
    if status:
        parts.append(f"HTTP {status.group(1)}")
    with suppress(json.JSONDecodeError, TypeError):
        graphql = _graphql_error_detail(json.loads(result.stdout))
        if graphql:
            parts.append(graphql)
    return "; ".join(parts) or f"exit code {result.returncode}"


@contextmanager
def _step(name: str) -> Iterator[None]:
    """Label any GitHub API failure inside the block with the failing policy step."""
    try:
        yield
    except GitHubAPIError as exc:
        exc.step = exc.step or name
        raise


def api_failure_message(exc: GitHubAPIError) -> str:
    """Build the fail-closed refusal message from sanitized failure facts."""
    step = f"step: {exc.step}" if exc.step else ""
    facts = "; ".join(fact for fact in (step, exc.detail) if fact)
    suffix = f" ({facts})" if facts else ""
    return f"Scoped merge refused: validation could not be completed safely{suffix}."


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
        "capabilities",
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
    capabilities = _load_capabilities(document["capabilities"])
    referenced_checks = required_checks | frozenset(
        check_id
        for capability in capabilities.values()
        for check_id in capability.additional_checks
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
        capabilities=capabilities,
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


def _load_capabilities(raw_capabilities: object) -> Mapping[str, Capability]:
    capabilities_document = _mapping(raw_capabilities, "capabilities")
    if not capabilities_document:
        _refuse("policy must define at least one capability")
    capabilities: dict[str, Capability] = {}
    for name, capability_value in capabilities_document.items():
        if not _identifier(name):
            _refuse(f"capability name {name!r} is invalid")
        capability_document = _mapping(capability_value, f"capabilities.{name}")
        expected_fields = {"members", "prefixes", "paths", "additional_checks"}
        if set(capability_document) != expected_fields:
            _refuse(f"capabilities.{name} keys must be exactly {sorted(expected_fields)}")
        members = _member_collection(
            capability_document["members"], name=f"capabilities.{name}.members"
        )
        prefixes = _string_collection(
            capability_document["prefixes"], name=f"capabilities.{name}.prefixes"
        )
        paths = _string_collection(capability_document["paths"], name=f"capabilities.{name}.paths")
        additional_checks = frozenset(
            _string_collection(
                capability_document["additional_checks"],
                name=f"capabilities.{name}.additional_checks",
            )
        )
        if not prefixes and not paths:
            _refuse(f"capabilities.{name} must match at least one path")
        if any(not prefix.endswith("/") or not _safe_path(prefix[:-1]) for prefix in prefixes):
            _refuse(f"capabilities.{name} prefixes must be safe repository paths ending in '/'")
        if any(not _safe_path(path_value) for path_value in paths):
            _refuse(f"capabilities.{name} paths must be safe repository paths")
        capabilities[name] = Capability(
            members=members,
            prefixes=prefixes,
            paths=frozenset(paths),
            additional_checks=additional_checks,
        )
    return capabilities


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


def authorize_membership(
    policy: Policy, *, actor: str, role: RepositoryRole, required: frozenset[str]
) -> None:
    """Require membership in every selected capability unless the caller is an admin."""
    if role == "admin":
        return
    login = actor.casefold()
    missing = sorted(
        capability_name
        for capability_name in required
        if login not in policy.capabilities[capability_name].members
    )
    if missing:
        _refuse(f"@{actor} is not a member of required capabilities: {missing}")


def authorize_repository_access(permission: object, *, actor: str) -> RepositoryRole:
    """Return the command issuer's validated live repository role."""
    document = _mapping(permission, "repository permission")
    # GitHub's legacy ``permission`` field collapses Triage into ``read`` and
    # Maintain into ``write``. ``role_name`` preserves the actual role.
    role = document.get("role_name")
    if role not in {"triage", "write", "maintain", "admin"}:
        _refuse(f"@{actor} no longer has Triage or higher repository access")
    return cast("RepositoryRole", role)


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
            matching = {
                capability_name: capability
                for capability_name, capability in policy.capabilities.items()
                if _capability_matches(name, capability)
            }
            if not matching:
                _refuse(f"{name} does not match a delegated merge capability")
            required_capabilities.update(matching)
            required_checks.update(
                check_id
                for capability in matching.values()
                for check_id in capability.additional_checks
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


QUEUE_STATE_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $branch: String!) {
  repository(owner: $owner, name: $name) {
    mergeQueue(branch: $branch) { id }
    pullRequest(number: $number) { id isInMergeQueue }
  }
}
"""
ENQUEUE_MUTATION = """
mutation($id: ID!, $sha: GitObjectID!) {
  enqueuePullRequest(input: {pullRequestId: $id, expectedHeadOid: $sha}) {
    mergeQueueEntry { id }
  }
}
"""


def read_queue_state(api: GitHubClient, *, policy: Policy, number: int) -> QueueState:
    """Read whether the base branch has a merge queue and whether the PR is in it.

    A merge queue that exists on the branch is treated as required: GitHub only
    configures one through a branch rule. Any malformed answer fails closed.
    """
    owner, _, name = policy.repository.partition("/")
    data = _mapping(
        api.graphql(
            QUEUE_STATE_QUERY,
            {"owner": owner, "name": name, "number": number, "branch": policy.base_branch},
        ),
        "GraphQL data",
    )
    repository = _mapping(data.get("repository"), "GraphQL repository")
    pull = _mapping(repository.get("pullRequest"), "GraphQL pull request")
    in_queue = pull.get("isInMergeQueue")
    if not isinstance(in_queue, bool) or "mergeQueue" not in repository:
        _refuse("GitHub returned an unreadable merge queue state")
    return QueueState(
        pull_request_id=_string(pull.get("id"), "GraphQL pull request id"),
        queue_required=repository["mergeQueue"] is not None,
        in_queue=in_queue,
    )


@dataclass(frozen=True)
class DirectMerge:
    """Land through the pull request merge API (base branch has no merge queue)."""

    api: GitHubClient

    def land(self, request: LandingRequest) -> Merged:
        """Squash-merge atomically against the validated head SHA."""
        repository = quote(request.repository, safe="/")
        with _step("merge request"):
            written = self.api.write(
                f"repos/{repository}/pulls/{request.number}/merge",
                method="PUT",
                payload={"sha": request.head_sha, "merge_method": request.merge_method},
            )
        response = _mapping(written, "merge response")
        if response.get("merged") is not True:
            _refuse("GitHub refused the merge")
        return Merged(_string(response.get("sha"), "merge response SHA"))


@dataclass(frozen=True)
class QueueEnqueue:
    """Land through the base branch merge queue; the queue's CI is the final gate."""

    api: GitHubClient

    def land(self, request: LandingRequest) -> Enqueued:
        """Enqueue, letting GitHub reject the request if the head moved."""
        with _step("enqueue request"):
            answer = self.api.graphql(
                ENQUEUE_MUTATION, {"id": request.node_id, "sha": request.head_sha}
            )
        data = _mapping(answer, "GraphQL data")
        payload = _mapping(data.get("enqueuePullRequest"), "enqueue response")
        entry = _mapping(payload.get("mergeQueueEntry"), "merge queue entry")
        _string(entry.get("id"), "merge queue entry id")
        return Enqueued()


@dataclass(frozen=True)
class StackedQueueEnqueue:
    """Enqueue a pull request that is the bottom of a native GitHub stack.

    GitHub rejects the ``enqueuePullRequest`` mutation for stack members and
    requires the asynchronous merge REST API. That API merges every pull request
    in the stack up to and including the requested one, so ``select_strategy``
    only routes the bottom pull request (position 1) here: the one pull request
    whose scope was validated is then the only one landed.
    """

    api: GitHubClient

    def land(self, request: LandingRequest) -> Enqueued | AlreadyQueued:
        """Request an asynchronous merge-queue entry pinned to the validated head."""
        repository = quote(request.repository, safe="/")
        with _step("async enqueue request"):
            written = self.api.write(
                f"repos/{repository}/pulls/{request.number}/merge-async",
                method="PUT",
                payload={
                    "sha": request.head_sha,
                    "merge_method": request.merge_method,
                    "merge_action": "merge_queue",
                },
            )
        response = _mapping(written, "async merge response")
        details = _mapping(response.get("details"), "async merge details")
        status = response.get("status")
        if status == "enqueued":
            return AlreadyQueued()
        if status != "pending":
            _refuse("GitHub did not accept the asynchronous merge request")
        _string(details.get("uuid"), "async merge request id")
        if details.get("expected_head_sha") != request.head_sha:
            _refuse("GitHub pinned the asynchronous merge to a different head")
        if details.get("merge_action") not in {"default", "merge_queue"}:
            _refuse("GitHub selected a direct merge for the asynchronous request")
        return Enqueued()


def read_stack_position(pull: object) -> int | None:
    """Return this pull request's one-based stack position, or None if unstacked.

    A present but unreadable ``stack`` object fails closed rather than being
    treated as unstacked, since the wrong strategy would merge unvalidated work.
    """
    raw = _mapping(pull, "pull request").get("stack")
    if raw is None:
        return None
    stack = _mapping(raw, "pull request stack")
    return _positive_int(stack.get("position"), "pull request stack position")


def choose_strategy(
    state: QueueState, api: GitHubClient, *, stack_position: int | None = None
) -> LandingStrategy:
    """Pick the landing mode from the observed queue state and stack membership."""
    if stack_position is None:
        return QueueEnqueue(api) if state.queue_required else DirectMerge(api)
    if stack_position != 1:
        _refuse("only the bottom pull request of a stack can be merged; land the lower ones first")
    if not state.queue_required:
        _refuse("stacked pull requests are only supported when the base branch has a merge queue")
    return StackedQueueEnqueue(api)


def run(event_path: Path, *, api: GitHubClient, landing_api: GitHubClient | None) -> LandOutcome:
    """Validate every gate, then land through the strategy the base branch needs.

    ``api`` performs every read and the audit comment. ``landing_api`` performs
    only the landing writes. Without it the command is refused after
    authorization and before any landing write; there is no fallback to ``api``.
    """
    policy = load_policy()
    event = parse_event(json.loads(event_path.read_text()))
    authorize_event(event, policy)
    repository = quote(policy.repository, safe="/")
    pull_endpoint = f"repos/{repository}/pulls/{event.number}"
    actor = quote(event.actor, safe="")
    with _step("role check"):
        permission = api.get(f"repos/{repository}/collaborators/{actor}/permission")
    role = authorize_repository_access(permission, actor=event.actor)
    with _step("queue state read"):
        queue = read_queue_state(api, policy=policy, number=event.number)
    if queue.in_queue:
        return AlreadyQueued()
    with _step("PR fetch"):
        pull = api.get(pull_endpoint)
    head_sha = authorize_pull_request(pull, policy=policy, expected_number=event.number)
    changed_files = _mapping(pull, "pull request").get("changed_files")
    with _step("changed files read"):
        files = api.get(f"{pull_endpoint}/files?per_page=100", paginate=True)
    required_capabilities, required_checks = authorize_files(
        files,
        changed_files=changed_files,
        policy=policy,
    )
    authorize_membership(policy, actor=event.actor, role=role, required=required_capabilities)
    query = urlencode({"head_sha": head_sha, "event": "pull_request", "per_page": 100})
    for check_id in sorted(required_checks):
        check = policy.checks[check_id]
        workflow = quote(check.workflow_file, safe="")
        with _step("check-run lookup"):
            runs = api.get(f"repos/{repository}/actions/workflows/{workflow}/runs?{query}")
        run_id = select_workflow_run(runs, head_sha=head_sha, check_id=check_id)
        with _step("check-job read"):
            jobs = api.get(
                f"repos/{repository}/actions/runs/{run_id}/jobs?per_page=100", paginate=True
            )
        authorize_check_job(
            jobs,
            check_id=check_id,
            job_name=check.job_name,
        )
    with _step("PR refresh"):
        refreshed = api.get(pull_endpoint)
    refreshed_sha = authorize_pull_request(refreshed, policy=policy, expected_number=event.number)
    if refreshed_sha != head_sha:
        _refuse("the pull request changed during validation; rerun the command")
    if landing_api is None:
        _refuse(
            f"the landing token is not configured ({LANDING_TOKEN_ENV} is empty); "
            "see .github/delegated-merge.md, Setup"
        )
    strategy = choose_strategy(queue, landing_api, stack_position=read_stack_position(refreshed))
    return strategy.land(
        LandingRequest(
            repository=policy.repository,
            number=event.number,
            node_id=queue.pull_request_id,
            head_sha=head_sha,
            base_branch=policy.base_branch,
            merge_method=policy.merge_method,
        )
    )


def _success_message(outcome: Merged | Enqueued) -> str:
    if isinstance(outcome, Merged):
        return f"Scoped merge completed at `{outcome.sha}`."
    return (
        "Scoped merge enqueued: all delegated checks passed and the pull request was "
        "added to the merge queue. It is not merged yet; the queue's own CI decides."
    )


def _comment(api: GitHubClient, event: Event, body: str) -> None:
    repository = quote(event.repository, safe="/")
    api.write(
        f"repos/{repository}/issues/{event.number}/comments",
        method="POST",
        payload={"body": body},
    )


def _capability_matches(path: str, capability: Capability) -> bool:
    return path in capability.paths or any(
        path.startswith(prefix) for prefix in capability.prefixes
    )


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


def _member_collection(value: object, *, name: str) -> frozenset[str]:
    members = _string_collection(value, name=name)
    normalized = tuple(member.casefold() for member in members)
    if any(not _github_login(member) for member in members):
        _refuse(f"{name} must contain only valid GitHub logins")
    if len(normalized) != len(set(normalized)):
        _refuse(f"{name} contains case-insensitive duplicates")
    return frozenset(normalized)


def _github_login(value: str) -> bool:
    return (
        len(value) <= MAX_GITHUB_LOGIN_LENGTH
        and re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*", value) is not None
    )


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
    landing_api = landing_client()
    event: Event | None = None
    try:
        event = parse_event(json.loads(args.event.read_text()))
        if event.body.strip() != load_policy().command:
            return 0
        outcome = run(args.event, api=api, landing_api=landing_api)
    except MergeRefusalError as exc:
        message = f"Scoped merge refused: {exc}."
    except GitHubAPIError as exc:
        message = api_failure_message(exc)
    except (OSError, json.JSONDecodeError, tomllib.TOMLDecodeError):
        message = "Scoped merge refused: validation could not be completed safely."
    else:
        if isinstance(outcome, AlreadyQueued):
            # The earlier command already posted the audit comment.
            print("Pull request is already in the merge queue.")
            return 0
        message = _success_message(outcome)
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
