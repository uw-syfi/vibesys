"""Contracts for capability-scoped pull request merging."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping  # noqa: TC003  # runtime Protocol conformance
from dataclasses import replace
from pathlib import Path

import pytest
import yaml
from scripts import delegated_merge
from scripts.delegated_merge import (
    Capability,
    Check,
    Event,
    GitHubAPI,
    GitHubAPIError,
    MergeRefusalError,
    Policy,
    authorize_check_job,
    authorize_event,
    authorize_files,
    authorize_membership,
    authorize_pull_request,
    authorize_repository_access,
    load_policy,
    run,
    select_workflow_run,
)

REPO_ROOT = Path(__file__).parents[2]


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


def test_policy_accepts_only_exact_delegated_paths_and_both_sides_of_renames() -> None:
    policy = load_policy()
    assert all(not capability.members for capability in policy.capabilities.values())
    capabilities, checks = authorize_files(
        [[{"filename": "clients/tui/src/view.ts", "previous_filename": "src/server/view.py"}]],
        changed_files=1,
        policy=policy,
    )
    assert capabilities == {"tui", "server"}
    assert checks == {"pr-ci"}

    for filename in [
        "clients/tui/package.json",
        "clients/tuition/view.ts",
        "src/entrypoints/server.py",
        "src/server/../vibesys/core.py",
        "src/server\\escape.py",
    ]:
        with pytest.raises(MergeRefusalError, match=r"does not match|safe repository"):
            authorize_files([[{"filename": filename}]], changed_files=1, policy=policy)

    for controlled_path in [
        ".github/delegated-merge.toml",
        ".github/workflows/delegated-merge.yml",
        ".github/workflows/test.yml",
        "scripts/delegated_merge.py",
    ]:
        with pytest.raises(MergeRefusalError, match="controlled by"):
            authorize_files([[{"filename": controlled_path}]], changed_files=1, policy=policy)

    with pytest.raises(MergeRefusalError, match=r"old/location\.py"):
        authorize_files(
            [[{"filename": "src/server/location.py", "previous_filename": "old/location.py"}]],
            changed_files=1,
            policy=policy,
        )


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ("schema_version = 1", "schema_version = 2", "schema_version"),
        ('command = "/merge-scoped"', 'command = "/land"', "must start"),
        ('required_checks = ["pr-ci"]', "required_checks = []", "at least one"),
        ('workflow_file = "test.yml"', 'workflow_file = "../test.yml"', "workflow filename"),
        ("[capabilities.tui]", '[capabilities."not valid"]', "capability name"),
        (
            "additional_checks = []",
            'additional_checks = ["undefined"]',
            "undefined checks",
        ),
    ],
)
def test_policy_rejects_invalid_contracts(tmp_path: Path, old: str, new: str, message: str) -> None:
    policy_path = tmp_path / "policy.toml"
    policy_path.write_text(
        (REPO_ROOT / ".github" / "delegated-merge.toml").read_text().replace(old, new, 1)
    )

    with pytest.raises(MergeRefusalError, match=message):
        load_policy(policy_path)


def test_path_matches_union_every_matching_capability_and_check() -> None:
    policy = load_policy()
    overlapping = Policy(
        schema_version=policy.schema_version,
        command=policy.command,
        repository=policy.repository,
        base_branch=policy.base_branch,
        merge_method=policy.merge_method,
        required_checks=policy.required_checks,
        checks={**policy.checks, "security": Check("security.yml", "Security")},
        capabilities={
            **policy.capabilities,
            "platform": Capability(
                members=frozenset({"maintainer"}),
                prefixes=("src/",),
                paths=frozenset(),
                additional_checks=frozenset({"security"}),
            ),
        },
    )

    capabilities, checks = authorize_files(
        [[{"filename": "src/server/events.py"}]], changed_files=1, policy=overlapping
    )

    assert capabilities == {"server", "platform"}
    assert checks == {"pr-ci", "security"}


def test_file_validation_fails_closed_on_empty_truncated_or_oversized_diffs() -> None:
    policy = load_policy()
    with pytest.raises(MergeRefusalError, match="positive integer"):
        authorize_files([], changed_files=0, policy=policy)
    with pytest.raises(MergeRefusalError, match="returned 1 of 2"):
        authorize_files([[{"filename": "src/server/a.py"}]], changed_files=2, policy=policy)
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
        authorize_event(_event(**changes), load_policy())


def test_membership_is_case_insensitive_and_requires_every_capability(tmp_path: Path) -> None:
    policy_path = tmp_path / "policy.toml"
    policy_path.write_text(
        (REPO_ROOT / ".github" / "delegated-merge.toml")
        .read_text()
        .replace("members = []", 'members = ["MainTainer"]')
    )
    policy = load_policy(policy_path)
    assert all(capability.members == {"maintainer"} for capability in policy.capabilities.values())

    authorize_membership(
        policy,
        actor="MainTainer",
        role="triage",
        required=frozenset({"tui", "server"}),
    )
    authorize_membership(
        policy, actor="unlisted-admin", role="admin", required=frozenset({"tui", "server"})
    )
    for role in ["triage", "write", "maintain"]:
        with pytest.raises(MergeRefusalError, match="not a member"):
            authorize_membership(
                policy,
                actor="unlisted-user",
                role=role,
                required=frozenset({"server"}),
            )


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
    policy_path.write_text(
        (REPO_ROOT / ".github" / "delegated-merge.toml")
        .read_text()
        .replace("members = []", f"members = {members}", 1)
    )

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
        authorize_pull_request(_pull(**changes), policy=load_policy(), expected_number=42)


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

    def __init__(self, *, refreshed_sha: str = "abc123") -> None:
        self.refreshed_sha = refreshed_sha
        self.pull_reads = 0
        self.writes: list[tuple[str, str, dict[str, object]]] = []

    def get(self, endpoint: str, *, paginate: bool = False) -> object:
        if endpoint.endswith("/collaborators/maintainer/permission"):
            return {"permission": "read", "role_name": "triage"}
        if endpoint == "repos/uw-syfi/vibesys/pulls/42":
            self.pull_reads += 1
            return _pull(head={"sha": "abc123" if self.pull_reads == 1 else self.refreshed_sha})
        if endpoint.endswith("/files?per_page=100") and paginate:
            return [[{"filename": "src/server/events.py"}]]
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
        return {"merged": True, "sha": "merge456"}


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


def _authorize_server_member(monkeypatch: pytest.MonkeyPatch) -> None:
    policy = load_policy()
    server = replace(policy.capabilities["server"], members=frozenset({"maintainer"}))
    authorized = replace(policy, capabilities={**policy.capabilities, "server": server})
    monkeypatch.setattr(delegated_merge, "load_policy", lambda: authorized)


def test_complete_merge_rechecks_head_and_sends_atomic_squash_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_path = tmp_path / "event.json"
    _write_event(event_path)
    _authorize_server_member(monkeypatch)
    api = FakeGitHubAPI()

    assert run(event_path, api=api) == "merge456"
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
    _authorize_server_member(monkeypatch)
    api = FakeGitHubAPI(refreshed_sha="new789")

    with pytest.raises(MergeRefusalError, match="changed during validation"):
        run(event_path, api=api)

    assert api.writes == []


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
        "pull-requests": "read",
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
