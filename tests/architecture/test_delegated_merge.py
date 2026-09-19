"""Contracts for capability-scoped pull request merging."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping  # noqa: TC003  # runtime Protocol conformance
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st
from scripts import delegated_merge
from scripts.delegated_merge import (
    AlreadyQueued,
    Capability,
    Check,
    DirectMerge,
    Enqueued,
    Event,
    GitHubAPI,
    GitHubAPIError,
    LandingRequest,
    LandingStrategy,
    Merged,
    MergeRefusalError,
    Policy,
    QueueEnqueue,
    QueueState,
    RepositoryRole,
    api_failure_message,
    authorize_check_job,
    authorize_event,
    authorize_files,
    authorize_membership,
    authorize_pull_request,
    authorize_repository_access,
    choose_strategy,
    load_policy,
    read_queue_state,
    run,
    select_workflow_run,
)

REPO_ROOT = Path(__file__).parents[2]


def _policy() -> Policy:
    return Policy(
        schema_version=1,
        command="/merge-scoped",
        repository="uw-syfi/vibesys",
        base_branch="main",
        merge_method="squash",
        required_checks=frozenset({"pr-ci"}),
        checks={"pr-ci": Check("test.yml", "Required PR CI")},
        capabilities={
            "core": Capability(
                members=frozenset({"maintainer"}),
                prefixes=("owned/core/",),
                paths=frozenset(),
                additional_checks=frozenset(),
            )
        },
    )


def _policy_toml(*, members: str = '["MainTainer"]') -> str:
    return f"""schema_version = 1
command = "/merge-scoped"
repository = "example/repository"
base_branch = "main"
merge_method = "squash"
required_checks = ["pr-ci"]

[checks.pr-ci]
workflow_file = "test.yml"
job_name = "Required PR CI"

[capabilities.core]
members = {members}
prefixes = ["owned/core/"]
paths = []
additional_checks = []
"""


@dataclass(frozen=True)
class _FileAuthorizationCase:
    policy: Policy
    entries: tuple[dict[str, str], ...]
    allowed_names: tuple[str, ...]
    page_size: int


@st.composite
def _file_authorization_cases(draw) -> _FileAuthorizationCase:  # noqa: ANN001
    capability_count = draw(st.integers(min_value=1, max_value=5))
    capability_names = [f"cap{index}" for index in range(capability_count)]
    check_ids = [f"extra{index}" for index in range(capability_count)]
    shared = set(
        draw(
            st.lists(
                st.sampled_from(capability_names),
                min_size=min(2, capability_count),
                max_size=capability_count,
                unique=True,
            )
        )
    )
    capabilities: dict[str, Capability] = {}
    allowed_names: list[str] = []
    for capability_name in capability_names:
        prefixes = [f"owned/{capability_name}/"]
        if capability_name in shared:
            prefixes.append("shared/")
        exact_path = f"exact/{capability_name}.txt"
        allowed_names.extend((f"owned/{capability_name}/file.txt", exact_path))
        capabilities[capability_name] = Capability(
            members=frozenset({"maintainer"}),
            prefixes=tuple(prefixes),
            paths=frozenset({exact_path}),
            additional_checks=frozenset(
                draw(st.sets(st.sampled_from(check_ids), max_size=capability_count))
            ),
        )
    if shared:
        allowed_names.append("shared/file.txt")
    checks = {"base": Check("base.yml", "Base")}
    checks.update(
        {
            check_id: Check(f"{check_id}.yml", f"Extra {index}")
            for index, check_id in enumerate(check_ids)
        }
    )
    policy = Policy(
        schema_version=1,
        command="/merge-test",
        repository="example/repository",
        base_branch="main",
        merge_method="squash",
        required_checks=frozenset({"base"}),
        checks=checks,
        capabilities=capabilities,
    )
    name_strategy = st.sampled_from(allowed_names)
    entries = tuple(
        draw(
            st.lists(
                st.builds(
                    lambda filename, previous: (
                        {"filename": filename}
                        if previous is None
                        else {"filename": filename, "previous_filename": previous}
                    ),
                    filename=name_strategy,
                    previous=st.one_of(st.none(), name_strategy),
                ),
                min_size=1,
                max_size=12,
            )
        )
    )
    return _FileAuthorizationCase(
        policy=policy,
        entries=entries,
        allowed_names=tuple(allowed_names),
        page_size=draw(st.integers(min_value=1, max_value=len(entries))),
    )


def _expected_file_authorization(
    policy: Policy, entries: tuple[dict[str, str], ...]
) -> tuple[frozenset[str], frozenset[str]]:
    names = [
        name
        for entry in entries
        for name in (entry["filename"], entry.get("previous_filename"))
        if name is not None
    ]
    capabilities = frozenset(
        capability_name
        for capability_name, capability in policy.capabilities.items()
        if any(
            name in capability.paths
            or any(name.startswith(prefix) for prefix in capability.prefixes)
            for name in names
        )
    )
    checks = policy.required_checks | frozenset(
        check_id
        for capability_name in capabilities
        for check_id in policy.capabilities[capability_name].additional_checks
    )
    return capabilities, checks


@st.composite
def _membership_cases(
    draw,  # noqa: ANN001
) -> tuple[Policy, str, RepositoryRole, frozenset[str]]:
    capability_count = draw(st.integers(min_value=1, max_value=6))
    names = [f"cap{index}" for index in range(capability_count)]
    login = draw(st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=12))
    uppercase_index = draw(st.integers(min_value=0, max_value=len(login) - 1))
    actor = (
        f"{login[:uppercase_index]}{login[uppercase_index].upper()}{login[uppercase_index + 1 :]}"
    )
    memberships = draw(
        st.dictionaries(
            keys=st.sampled_from(names),
            values=st.booleans(),
            min_size=capability_count,
            max_size=capability_count,
        )
    )
    capabilities = {
        name: Capability(
            members=frozenset({login} if memberships[name] else {"other-user"}),
            prefixes=(f"owned/{name}/",),
            paths=frozenset(),
            additional_checks=frozenset(),
        )
        for name in names
    }
    policy = Policy(
        schema_version=1,
        command="/merge-test",
        repository="example/repository",
        base_branch="main",
        merge_method="squash",
        required_checks=frozenset({"base"}),
        checks={"base": Check("base.yml", "Base")},
        capabilities=capabilities,
    )
    required = frozenset(draw(st.sets(st.sampled_from(names), max_size=capability_count)))
    role: RepositoryRole = draw(st.sampled_from(["triage", "write", "maintain", "admin"]))
    return policy, actor, role, required


def _event(**changes: object) -> Event:
    values: dict[str, object] = {
        "repository": "uw-syfi/vibesys",
        "number": 42,
        "actor": "maintainer",
        "actor_type": "User",
        "action": "created",
        "body": "/merge-scoped",
        "is_pull_request": True,
    }
    values.update(changes)
    return Event(**values)  # type: ignore[arg-type]


def _pull(**changes: object) -> dict[str, object]:
    values: dict[str, object] = {
        "number": 42,
        "state": "open",
        "merged": False,
        "draft": False,
        "mergeable": True,
        "mergeable_state": "clean",
        "changed_files": 1,
        "base": {"ref": "main", "repo": {"full_name": "uw-syfi/vibesys"}},
        "head": {"sha": "abc123"},
        "user": {"login": "author"},
    }
    values.update(changes)
    return values


def test_checked_in_policy_loads_and_hard_denies() -> None:
    policy = load_policy()
    controlled_paths = delegated_merge.HARD_DENIED_PATHS | {
        f".github/workflows/{check.workflow_file}" for check in policy.checks.values()
    }
    for controlled_path in controlled_paths:
        with pytest.raises(MergeRefusalError, match="controlled by"):
            authorize_files([[{"filename": controlled_path}]], changed_files=1, policy=policy)


@pytest.mark.parametrize(
    "filenames",
    [
        ["docs/contributing/tui/README.md"],
        ["docs/contributing/tui/conventions.md", "clients/tui/src/app.ts"],
    ],
)
def test_checked_in_policy_lets_tui_members_land_tui_docs(filenames: list[str]) -> None:
    policy = load_policy()
    entries = [{"filename": name} for name in filenames]

    capabilities, _checks = authorize_files([entries], changed_files=len(entries), policy=policy)

    assert capabilities == {"tui"}
    assert {"ayanbinrafaih", "nano-ai"} <= policy.capabilities["tui"].members


@pytest.mark.parametrize(
    "filename", ["docs/contributing/other.md", "docs/contributing/tui-conventions.md"]
)
def test_checked_in_policy_keeps_other_contributing_docs_out_of_scope(filename: str) -> None:
    policy = load_policy()

    with pytest.raises(MergeRefusalError, match="does not match"):
        authorize_files([[{"filename": filename}]], changed_files=1, policy=policy)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("schema_version = 1", "schema_version = 2", "schema_version"),
        ('command = "/merge-scoped"', 'command = "/land"', "must start"),
        ('required_checks = ["pr-ci"]', "required_checks = []", "at least one"),
        ('workflow_file = "test.yml"', 'workflow_file = "../test.yml"', "workflow filename"),
        ("[capabilities.core]", '[capabilities."not valid"]', "capability name"),
        (
            "additional_checks = []",
            'additional_checks = ["undefined"]',
            "undefined checks",
        ),
    ],
)
def test_policy_rejects_invalid_contracts(tmp_path: Path, old: str, new: str, message: str) -> None:
    policy_path = tmp_path / "policy.toml"
    policy_path.write_text(_policy_toml().replace(old, new, 1))

    with pytest.raises(MergeRefusalError, match=message):
        load_policy(policy_path)


@given(case=_file_authorization_cases())
def test_file_authorization_returns_exact_union_independent_of_order_and_pages(
    case: _FileAuthorizationCase,
) -> None:
    expected = _expected_file_authorization(case.policy, case.entries)
    pages = [
        list(case.entries[index : index + case.page_size])
        for index in range(0, len(case.entries), case.page_size)
    ]

    assert (
        authorize_files([list(case.entries)], changed_files=len(case.entries), policy=case.policy)
        == expected
    )
    assert authorize_files(pages, changed_files=len(case.entries), policy=case.policy) == expected
    assert (
        authorize_files(
            [list(reversed(case.entries))], changed_files=len(case.entries), policy=case.policy
        )
        == expected
    )


@given(case=_file_authorization_cases(), first=st.integers(), second=st.integers())
def test_rename_authorization_equals_the_union_of_both_paths(
    case: _FileAuthorizationCase, first: int, second: int
) -> None:
    current = case.allowed_names[first % len(case.allowed_names)]
    previous = case.allowed_names[second % len(case.allowed_names)]

    renamed = authorize_files(
        [[{"filename": current, "previous_filename": previous}]],
        changed_files=1,
        policy=case.policy,
    )
    separate = authorize_files(
        [[{"filename": current}, {"filename": previous}]],
        changed_files=2,
        policy=case.policy,
    )

    assert renamed == separate


@pytest.mark.parametrize("kind", ["unmatched", "unsafe", "controlled"])
@given(
    case=_file_authorization_cases(),
    location=st.sampled_from(["filename", "previous_filename"]),
    unsafe_path=st.sampled_from(["", "/absolute", "a/../escape", "a/./file", "a//file", "a\\file"]),
)
def test_file_authorization_fails_closed_when_a_bad_path_is_injected(
    case: _FileAuthorizationCase, kind: str, location: str, unsafe_path: str
) -> None:
    if kind == "unmatched":
        bad_path = "unowned/file.txt"
    elif kind == "unsafe":
        bad_path = unsafe_path
    else:
        workflow_file = next(iter(case.policy.checks.values())).workflow_file
        bad_path = f".github/workflows/{workflow_file}"
    injected = {"filename": case.allowed_names[0] if location == "previous_filename" else bad_path}
    if location == "previous_filename":
        injected["previous_filename"] = bad_path
    entries = (*case.entries, injected)

    with pytest.raises(MergeRefusalError):
        authorize_files([list(entries)], changed_files=len(entries), policy=case.policy)


def test_file_validation_fails_closed_on_empty_truncated_or_oversized_diffs() -> None:
    policy = _policy()
    with pytest.raises(MergeRefusalError, match="positive integer"):
        authorize_files([], changed_files=0, policy=policy)
    with pytest.raises(MergeRefusalError, match="returned 1 of 2"):
        authorize_files([[{"filename": "owned/core/a.py"}]], changed_files=2, policy=policy)
    with pytest.raises(MergeRefusalError, match="3000-file"):
        authorize_files([], changed_files=3001, policy=policy)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"actor_type": "Bot"}, "must be a GitHub user"),
        ({"body": "/merge-scoped now"}, "exact command"),
        ({"body": "/merge-scoped "}, "exact command"),
        ({"repository": "other/repo"}, "not for"),
        ({"is_pull_request": False}, "pull request comment"),
    ],
)
def test_event_authorization_rejects_wrong_actor_or_scope(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(MergeRefusalError, match=message):
        authorize_event(_event(**changes), _policy())


def test_policy_normalizes_capability_members_case_insensitively(tmp_path: Path) -> None:
    policy_path = tmp_path / "policy.toml"
    policy_path.write_text(_policy_toml())
    policy = load_policy(policy_path)
    assert policy.capabilities["core"].members == {"maintainer"}


@given(case=_membership_cases())
def test_membership_succeeds_iff_admin_or_member_of_every_required_capability(
    case: tuple[Policy, str, RepositoryRole, frozenset[str]],
) -> None:
    policy, actor, role, required = case
    expected = role == "admin" or all(
        actor.casefold() in policy.capabilities[name].members for name in required
    )

    if expected:
        authorize_membership(policy, actor=actor, role=role, required=required)
    else:
        with pytest.raises(MergeRefusalError, match="not a member"):
            authorize_membership(policy, actor=actor, role=role, required=required)


@pytest.mark.parametrize(
    ("members", "message"),
    [
        ('["Alice", "alice"]', "case-insensitive duplicates"),
        ('["-alice"]', "valid GitHub logins"),
        ('["alice-"]', "valid GitHub logins"),
        ('["alice--bob"]', "valid GitHub logins"),
        ('["alice_user"]', "valid GitHub logins"),
        (f'["{"a" * 40}"]', "valid GitHub logins"),
        ("[1]", "non-empty strings"),
    ],
)
def test_policy_rejects_invalid_capability_members(
    tmp_path: Path, members: str, message: str
) -> None:
    policy_path = tmp_path / "policy.toml"
    policy_path.write_text(_policy_toml(members=members))

    with pytest.raises(MergeRefusalError, match=message):
        load_policy(policy_path)


def test_repository_access_is_checked_live() -> None:
    for role in ["triage", "write", "maintain", "admin"]:
        assert (
            authorize_repository_access(
                {"permission": "read", "role_name": role}, actor="maintainer"
            )
            == role
        )
    with pytest.raises(MergeRefusalError, match="no longer has Triage"):
        authorize_repository_access({"permission": "read", "role_name": "read"}, actor="maintainer")


def test_github_api_calls_have_a_bounded_timeout() -> None:
    def timeout_runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("gh", 30)

    with pytest.raises(GitHubAPIError, match="could not read"):
        GitHubAPI(_runner=timeout_runner).get("repos/uw-syfi/vibesys")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"draft": True}, "draft"),
        ({"state": "closed"}, "not open"),
        ({"mergeable": None}, "not currently clean"),
        ({"mergeable_state": "behind"}, "not currently clean"),
        ({"base": {"ref": "release", "repo": {"full_name": "uw-syfi/vibesys"}}}, "must target"),
    ],
)
def test_pull_request_authorization_requires_exact_clean_destination(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(MergeRefusalError, match=message):
        authorize_pull_request(_pull(**changes), policy=_policy(), expected_number=42)


def test_check_authorization_uses_latest_exact_head_run_and_exact_job() -> None:
    runs = {
        "workflow_runs": [
            {"id": 1, "head_sha": "old", "event": "pull_request"},
            {"id": 2, "head_sha": "abc123", "event": "push"},
            {"id": 3, "head_sha": "abc123", "event": "pull_request"},
            {"id": 4, "head_sha": "abc123", "event": "pull_request"},
        ]
    }
    assert select_workflow_run(runs, head_sha="abc123", check_id="pr-ci") == 4
    authorize_check_job(
        [{"jobs": [{"name": "Required PR CI", "status": "completed", "conclusion": "success"}]}],
        check_id="pr-ci",
        job_name="Required PR CI",
    )

    for jobs in [
        [{"jobs": []}],
        [{"jobs": [{"name": "Required PR CI", "status": "completed", "conclusion": "failure"}]}],
        [
            {
                "jobs": [
                    {"name": "Required PR CI", "status": "completed", "conclusion": "success"},
                    {"name": "Required PR CI", "status": "completed", "conclusion": "success"},
                ]
            }
        ],
    ]:
        with pytest.raises(MergeRefusalError):
            authorize_check_job(jobs, check_id="pr-ci", job_name="Required PR CI")


class FakeGitHubAPI:
    """Record the state transitions made by one complete merge attempt."""

    def __init__(  # noqa: PLR0913  # keyword-only scenario knobs
        self,
        *,
        refreshed_sha: str = "abc123",
        queue: object = None,
        in_queue: object = False,
        graphql_error: bool = False,
        enqueue_entry: object = None,
        state_data: object = "default",
        merge_response: object = None,
        filename: str = "owned/core/events.py",
    ) -> None:
        self.state_data = state_data
        self.merge_response = merge_response or {"merged": True, "sha": "merge456"}
        self.filename = filename
        self.refreshed_sha = refreshed_sha
        self.queue = queue
        self.in_queue = in_queue
        self.graphql_error = graphql_error
        self.enqueue_entry = {"id": "entry1"} if enqueue_entry is None else enqueue_entry
        self.graphql_calls: list[tuple[str, dict[str, object]]] = []
        self.pull_reads = 0
        self.writes: list[tuple[str, str, dict[str, object]]] = []

    def get(self, endpoint: str, *, paginate: bool = False) -> object:
        if endpoint.endswith("/collaborators/maintainer/permission"):
            return {"permission": "read", "role_name": "triage"}
        if endpoint == "repos/uw-syfi/vibesys/pulls/42":
            self.pull_reads += 1
            return _pull(head={"sha": "abc123" if self.pull_reads == 1 else self.refreshed_sha})
        if endpoint.endswith("/files?per_page=100") and paginate:
            return [[{"filename": self.filename}]]
        if "/actions/workflows/test.yml/runs?" in endpoint:
            return {
                "workflow_runs": [
                    {
                        "id": 7,
                        "head_sha": "abc123",
                        "event": "pull_request",
                    }
                ]
            }
        if endpoint.endswith("/actions/runs/7/jobs?per_page=100") and paginate:
            return [
                {
                    "jobs": [
                        {
                            "name": "Required PR CI",
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ]
                }
            ]
        pytest.fail(f"unexpected GET {endpoint}")

    def write(self, endpoint: str, *, method: str, payload: Mapping[str, object]) -> object:
        self.writes.append((endpoint, method, dict(payload)))
        return self.merge_response

    def graphql(self, query: str, variables: Mapping[str, object]) -> object:
        self.graphql_calls.append((query, dict(variables)))
        if self.graphql_error:
            raise GitHubAPIError.graphql_failed()
        if "enqueuePullRequest" in query:
            return {"enqueuePullRequest": {"mergeQueueEntry": self.enqueue_entry}}
        if self.state_data != "default":
            return self.state_data
        return {
            "repository": {
                "mergeQueue": self.queue,
                "pullRequest": {"id": "PR_node", "isInMergeQueue": self.in_queue},
            }
        }


def _write_event(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "action": "created",
                "repository": {"full_name": "uw-syfi/vibesys"},
                "issue": {"number": 42, "pull_request": {"url": "unused"}},
                "comment": {
                    "body": "/merge-scoped",
                    "user": {"login": "maintainer", "type": "User"},
                },
            }
        )
    )


def _use_test_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(delegated_merge, "load_policy", _policy)


def test_complete_merge_rechecks_head_and_sends_atomic_squash_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_path = tmp_path / "event.json"
    _write_event(event_path)
    _use_test_policy(monkeypatch)
    api = FakeGitHubAPI()

    assert run(event_path, api=api) == Merged("merge456")
    assert api.pull_reads == 2
    assert api.writes == [
        (
            "repos/uw-syfi/vibesys/pulls/42/merge",
            "PUT",
            {"sha": "abc123", "merge_method": "squash"},
        )
    ]


def test_complete_merge_refuses_head_change_before_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_path = tmp_path / "event.json"
    _write_event(event_path)
    _use_test_policy(monkeypatch)
    api = FakeGitHubAPI(refreshed_sha="new789")

    with pytest.raises(MergeRefusalError, match="changed during validation"):
        run(event_path, api=api)

    assert api.writes == []


def _land_request() -> LandingRequest:
    return LandingRequest(
        repository="uw-syfi/vibesys",
        number=42,
        node_id="PR_node",
        head_sha="abc123",
        base_branch="main",
        merge_method="squash",
    )


@pytest.mark.parametrize(
    ("strategy_type", "expected"),
    [(DirectMerge, Merged("merge456")), (QueueEnqueue, Enqueued())],
)
def test_strategies_share_one_landing_contract(
    strategy_type: type[DirectMerge] | type[QueueEnqueue], expected: Merged | Enqueued
) -> None:
    api = FakeGitHubAPI(queue={"id": "Q"})
    strategy: LandingStrategy = strategy_type(api)

    assert strategy.land(_land_request()) == expected


def test_strategies_refuse_an_unconfirmed_landing() -> None:
    with pytest.raises(MergeRefusalError, match="refused the merge"):
        DirectMerge(FakeGitHubAPI(merge_response={"merged": False})).land(_land_request())
    for entry in ("not-an-object", {}, {"id": ""}):
        api = FakeGitHubAPI(queue={"id": "Q"}, enqueue_entry=entry)
        with pytest.raises(MergeRefusalError):
            QueueEnqueue(api).land(_land_request())
    with pytest.raises(GitHubAPIError):
        QueueEnqueue(FakeGitHubAPI(graphql_error=True)).land(_land_request())


def test_selector_uses_enqueue_only_when_a_queue_is_observed() -> None:
    api = FakeGitHubAPI()
    assert isinstance(
        choose_strategy(QueueState("id", queue_required=False, in_queue=False), api), DirectMerge
    )
    assert isinstance(
        choose_strategy(QueueState("id", queue_required=True, in_queue=False), api), QueueEnqueue
    )


@pytest.mark.parametrize(
    "data",
    [
        None,
        {"repository": None},
        {"repository": {"mergeQueue": None}},
        {"repository": {"pullRequest": {"id": "x", "isInMergeQueue": False}}},
        {"repository": {"mergeQueue": None, "pullRequest": {"id": "x", "isInMergeQueue": "no"}}},
        {"repository": {"mergeQueue": None, "pullRequest": {"id": "", "isInMergeQueue": False}}},
    ],
)
def test_queue_state_fails_closed_on_unreadable_answers(data: object) -> None:
    api = FakeGitHubAPI(state_data=data)

    with pytest.raises(MergeRefusalError):
        read_queue_state(api, policy=_policy(), number=42)


def test_queue_required_enqueues_after_all_checks_without_a_merge_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_path = tmp_path / "event.json"
    _write_event(event_path)
    _use_test_policy(monkeypatch)
    api = FakeGitHubAPI(queue={"id": "Q"})

    assert run(event_path, api=api) == Enqueued()
    assert api.pull_reads == 2
    assert api.writes == []
    assert api.graphql_calls[-1][1] == {"id": "PR_node", "sha": "abc123"}


def test_queue_mode_keeps_scope_and_head_refusals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_path = tmp_path / "event.json"
    _write_event(event_path)
    _use_test_policy(monkeypatch)

    stale = FakeGitHubAPI(queue={"id": "Q"}, refreshed_sha="new789")
    with pytest.raises(MergeRefusalError, match="changed during validation"):
        run(event_path, api=stale)
    assert not any("enqueuePullRequest" in query for query, _ in stale.graphql_calls)

    out_of_scope = FakeGitHubAPI(queue={"id": "Q"}, filename="elsewhere/file.py")
    with pytest.raises(MergeRefusalError, match="does not match"):
        run(event_path, api=out_of_scope)
    assert len(out_of_scope.graphql_calls) == 1
    assert out_of_scope.writes == []


def test_already_queued_pull_request_is_a_noop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_path = tmp_path / "event.json"
    _write_event(event_path)
    _use_test_policy(monkeypatch)
    api = FakeGitHubAPI(queue={"id": "Q"}, in_queue=True)

    assert run(event_path, api=api) == AlreadyQueued()
    assert api.writes == []
    assert len(api.graphql_calls) == 1


def test_queue_lookup_failure_never_merges_or_enqueues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_path = tmp_path / "event.json"
    _write_event(event_path)
    _use_test_policy(monkeypatch)
    api = FakeGitHubAPI(graphql_error=True)

    with pytest.raises(GitHubAPIError):
        run(event_path, api=api)
    assert api.writes == []


class _FailingAPI(FakeGitHubAPI):
    """Fail one call, chosen by endpoint fragment or GraphQL operation."""

    def __init__(self, *, fail_on: str, error: GitHubAPIError, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.fail_on = fail_on
        self.error = error

    def get(self, endpoint: str, *, paginate: bool = False) -> object:
        if self.fail_on in endpoint:
            raise self.error
        return super().get(endpoint, paginate=paginate)

    def graphql(self, query: str, variables: Mapping[str, object]) -> object:
        if self.fail_on in query:
            raise self.error
        return super().graphql(query, variables)


@pytest.mark.parametrize(
    ("fail_on", "step", "queue"),
    [
        ("/permission", "role check", None),
        ("mergeQueue(", "queue state read", None),
        ("/pulls/42", "PR fetch", None),
        ("/files?", "changed files read", None),
        ("/workflows/test.yml/runs", "check-run lookup", None),
        ("/jobs?", "check-job read", None),
        ("enqueuePullRequest", "enqueue request", {"id": "Q"}),
    ],
)
def test_api_failure_names_the_failing_step_and_still_refuses(  # noqa: PLR0913
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fail_on: str,
    step: str,
    queue: object,
) -> None:
    event_path = tmp_path / "event.json"
    _write_event(event_path)
    _use_test_policy(monkeypatch)
    monkeypatch.setattr("sys.argv", ["delegated_merge", "--event", str(event_path)])
    error = GitHubAPIError.request_failed("read GitHub state", detail="HTTP 403")
    api = _FailingAPI(fail_on=fail_on, error=error, queue=queue)
    monkeypatch.setattr(delegated_merge, "GitHubAPI", lambda: api)

    code = delegated_merge.main()

    assert code == 1
    message = capsys.readouterr().err.strip()
    assert message == (
        f"Scoped merge refused: validation could not be completed safely (step: {step}; HTTP 403)."
    )
    assert not any(endpoint.endswith("/merge") for endpoint, _, _ in api.writes)
    if step != "enqueue request":
        assert not any("enqueuePullRequest" in query for query, _ in api.graphql_calls)


def test_pull_request_refresh_failure_is_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    error = GitHubAPIError.request_failed("read GitHub state")

    class RefreshFails(FakeGitHubAPI):
        def get(self, endpoint: str, *, paginate: bool = False) -> object:
            if endpoint == "repos/uw-syfi/vibesys/pulls/42" and self.pull_reads == 1:
                raise error
            return super().get(endpoint, paginate=paginate)

    event_path = tmp_path / "event.json"
    _write_event(event_path)
    _use_test_policy(monkeypatch)
    api = RefreshFails(queue={"id": "Q"})

    with pytest.raises(GitHubAPIError) as caught:
        run(event_path, api=api)
    assert caught.value.step == "PR refresh"
    assert api.writes == []


def test_gh_failures_expose_only_http_status_and_graphql_error_types() -> None:
    secret = "ghs_SECRETTOKEN0123"  # noqa: S105
    body = json.dumps(
        {
            "data": None,
            "errors": [
                {"type": "FORBIDDEN", "message": f"private {secret}"},
                {"type": "FORBIDDEN"},
                {"type": "not a type", "message": secret},
                {"message": secret},
            ],
        }
    )

    def failing(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["gh"], 1, stdout=body, stderr=f"gh: {secret} (HTTP 403)\n"
        )

    with pytest.raises(GitHubAPIError) as caught:
        GitHubAPI(_runner=failing).graphql("query", {})
    assert caught.value.detail == "HTTP 403; GraphQL FORBIDDEN"
    assert secret not in api_failure_message(caught.value)


@pytest.mark.parametrize(
    ("result", "detail"),
    [
        (subprocess.CompletedProcess(["gh"], 1, stdout="", stderr="boom"), "exit code 1"),
        (
            subprocess.CompletedProcess(["gh"], 1, stdout="<html>", stderr="x (HTTP 502)"),
            "HTTP 502",
        ),
        (
            subprocess.CompletedProcess(
                ["gh"], 0, stdout='{"errors":[{"type":"NOT_FOUND"}]}', stderr=""
            ),
            None,
        ),
    ],
)
def test_failure_detail_is_allow_listed(
    result: subprocess.CompletedProcess[str], detail: str | None
) -> None:
    def runner(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return result

    api = GitHubAPI(_runner=runner)
    if detail is None:
        with pytest.raises(GitHubAPIError) as caught:
            api.graphql("query", {})
        assert caught.value.detail == "GraphQL NOT_FOUND"
        return
    with pytest.raises(GitHubAPIError) as caught:
        api.get("repos/x/y")
    assert caught.value.detail == detail


def test_main_reports_enqueue_distinctly_from_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    event_path = tmp_path / "event.json"
    _write_event(event_path)
    _use_test_policy(monkeypatch)
    monkeypatch.setattr("sys.argv", ["delegated_merge", "--event", str(event_path)])
    results: list[tuple[FakeGitHubAPI, int, str]] = []
    fakes = (
        FakeGitHubAPI(queue={"id": "Q"}),
        FakeGitHubAPI(),
        FakeGitHubAPI(queue={"id": "Q"}, in_queue=True),
    )
    for api in fakes:
        monkeypatch.setattr(delegated_merge, "GitHubAPI", lambda api=api: api)
        code = delegated_merge.main()
        results.append((api, code, capsys.readouterr().out))

    (queued, queued_code, queued_out), (direct, _, direct_out), (dup, dup_code, _) = results
    assert queued_code == 0
    assert "enqueued" in queued_out
    assert "merge456" not in queued_out
    assert "not merged yet" in str(queued.writes[0][2]["body"])
    assert "completed at `merge456`" in direct_out
    assert direct.writes[0][1] == "PUT"
    assert dup_code == 0
    assert not any(endpoint.endswith("/comments") for endpoint, _, _ in dup.writes)


def test_workflow_uses_trusted_default_branch_and_pinned_actions() -> None:
    path = REPO_ROOT / ".github" / "workflows" / "delegated-merge.yml"
    text = path.read_text()
    workflow = yaml.safe_load(text)
    jobs = workflow["jobs"]
    steps = jobs["merge"]["steps"]

    assert "issue_comment:" in text
    assert "types: [created]" in text
    assert workflow["permissions"] == {
        "actions": "read",
        "contents": "write",
        "issues": "write",
        "pull-requests": "write",
    }
    assert "github.event.repository.default_branch" in text
    assert "startsWith(github.event.comment.body, '/merge-')" in text
    assert "/merge-scoped" not in text
    assert "persist-credentials: false" in text
    assert "github.event.pull_request.head" not in text
    assert "github.event.comment.body" not in "\n".join(step.get("run", "") for step in steps)
    for step in steps:
        if "uses" in step:
            reference = step["uses"].split("@", maxsplit=1)[1].split()[0]
            assert re.fullmatch(r"[0-9a-f]{40}", reference)
    assert "actions/create-github-app-token" not in text
    assert steps[-1]["env"] == {"GH_TOKEN": "${{ github.token }}"}


def test_test_workflow_exposes_required_ci_and_compatibility_alias() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "test.yml").read_text())
    gate = workflow["jobs"]["required-pr-ci"]
    assert gate["if"] == "always()"
    assert "typecheck" in gate["needs"]
    assert "ci-budget" not in gate["needs"]
    assert gate["name"] == "Required PR CI"
    alias = workflow["jobs"]["scoped-merge-gate"]
    assert alias["needs"] == "required-pr-ci"
    assert alias["name"] == "Scoped merge gate"
