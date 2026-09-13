"""Contracts for path-scoped pull request merging."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Mapping  # noqa: TC003  # runtime Protocol conformance
from pathlib import Path

import pytest
import yaml
from scripts.scoped_merge import (
    Event,
    GitHubAPI,
    GitHubAPIError,
    MergeRefusalError,
    authorize_event,
    authorize_files,
    authorize_pull_request,
    authorize_repository_access,
    authorize_workflow,
    load_policy,
    run,
)

REPO_ROOT = Path(__file__).parents[2]


def _event(**changes: object) -> Event:
    values: dict[str, object] = {
        "repository": "uw-syfi/vibesys",
        "number": 42,
        "actor": "maintainer",
        "actor_type": "User",
        "action": "created",
        "body": "/merge-tui",
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
    authorize_files(
        [[{"filename": "clients/tui/src/view.ts", "previous_filename": "src/server/view.py"}]],
        changed_files=1,
        policy=policy,
    )

    for filename in [
        "clients/tui/package.json",
        "clients/tuition/view.ts",
        "src/entrypoints/server.py",
        ".github/scoped-merge.toml",
        "src/server/../vibesys/core.py",
        "src/server\\escape.py",
    ]:
        with pytest.raises(MergeRefusalError, match="outside"):
            authorize_files([[{"filename": filename}]], changed_files=1, policy=policy)

    with pytest.raises(MergeRefusalError, match=r"old/location\.py"):
        authorize_files(
            [[{"filename": "src/server/location.py", "previous_filename": "old/location.py"}]],
            changed_files=1,
            policy=policy,
        )


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
        ({"actor": "stranger"}, "not an authorized"),
        ({"actor_type": "Bot"}, "not an authorized"),
        ({"body": "/merge-tui now"}, "exact command"),
        ({"repository": "other/repo"}, "not for"),
        ({"is_pull_request": False}, "pull request comment"),
    ],
)
def test_event_authorization_rejects_wrong_actor_or_scope(
    changes: dict[str, object], message: str
) -> None:
    with pytest.raises(MergeRefusalError, match=message):
        authorize_event(_event(**changes), load_policy(), "maintainer, someone-else")


def test_event_authorization_requires_configured_users() -> None:
    with pytest.raises(MergeRefusalError, match="no scoped merge maintainers"):
        authorize_event(_event(), load_policy(), "")


def test_repository_access_is_checked_live() -> None:
    authorize_repository_access({"permission": "read", "role_name": "triage"}, actor="maintainer")
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


def test_workflow_authorization_requires_latest_current_head_success() -> None:
    authorize_workflow(
        {
            "workflow_runs": [
                {
                    "id": 1,
                    "head_sha": "abc123",
                    "event": "pull_request",
                    "status": "completed",
                    "conclusion": "success",
                }
            ]
        },
        head_sha="abc123",
    )
    with pytest.raises(MergeRefusalError, match="has not succeeded"):
        authorize_workflow(
            {
                "workflow_runs": [
                    {
                        "id": 1,
                        "head_sha": "abc123",
                        "event": "pull_request",
                        "status": "completed",
                        "conclusion": "success",
                    },
                    {
                        "id": 2,
                        "head_sha": "abc123",
                        "event": "pull_request",
                        "status": "in_progress",
                        "conclusion": None,
                    },
                ]
            },
            head_sha="abc123",
        )


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
                        "status": "completed",
                        "conclusion": "success",
                    }
                ]
            }
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
                    "body": "/merge-tui",
                    "user": {"login": "maintainer", "type": "User"},
                },
            }
        )
    )


def test_complete_merge_rechecks_head_and_sends_atomic_squash_request(tmp_path: Path) -> None:
    event_path = tmp_path / "event.json"
    _write_event(event_path)
    api = FakeGitHubAPI()

    assert run(event_path, authorized_users="maintainer", api=api) == "merge456"
    assert api.pull_reads == 2
    assert api.writes == [
        (
            "repos/uw-syfi/vibesys/pulls/42/merge",
            "PUT",
            {"sha": "abc123", "merge_method": "squash"},
        )
    ]


def test_complete_merge_refuses_head_change_before_write(tmp_path: Path) -> None:
    event_path = tmp_path / "event.json"
    _write_event(event_path)
    api = FakeGitHubAPI(refreshed_sha="new789")

    with pytest.raises(MergeRefusalError, match="changed during validation"):
        run(event_path, authorized_users="maintainer", api=api)

    assert api.writes == []


def test_workflow_uses_trusted_default_branch_and_pinned_actions() -> None:
    path = REPO_ROOT / ".github" / "workflows" / "scoped-merge.yml"
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
    assert "persist-credentials: false" in text
    assert "github.event.pull_request.head" not in text
    assert "github.event.comment.body" not in "\n".join(step.get("run", "") for step in steps)
    for step in steps:
        if "uses" in step:
            reference = step["uses"].split("@", maxsplit=1)[1].split()[0]
            assert re.fullmatch(r"[0-9a-f]{40}", reference)
    assert "actions/create-github-app-token" not in text
    assert "GH_TOKEN: ${{ github.token }}" in text


def test_test_workflow_exposes_stable_scoped_merge_gate() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github" / "workflows" / "test.yml").read_text())
    gate = workflow["jobs"]["scoped-merge-gate"]
    assert gate["if"] == "always()"
    assert "typecheck" in gate["needs"]
    assert "ci-budget" not in gate["needs"]
