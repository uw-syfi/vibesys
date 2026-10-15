"""Tests for the GitHub CLI abstraction."""

from __future__ import annotations

from typing import TYPE_CHECKING, Never

import pytest

from vs_github.api import (
    GitHubAuthenticationError,
    GitHubCLI,
    GitHubCLIError,
    GitHubCLIUnavailableError,
)
from vs_sim.api.testing import FakeProcesses, ManualClock, ProcessScript

if TYPE_CHECKING:
    from pathlib import Path

    from vs_sim.api import ProcessSpec


def _scripted(results: list[ProcessScript]) -> FakeProcesses:
    """Processes that end each run in turn as ``results`` say, recording what ran."""
    remaining = iter(results)
    return FakeProcesses(lambda _spec: next(remaining), pass_time=ManualClock().advance)


def _calls(processes: FakeProcesses) -> list[tuple[list[str], Path | None]]:
    return [(list(spec.argv), spec.cwd) for spec in processes.ran]


def _result(returncode: int = 0, stdout: str = "", stderr: str = "") -> ProcessScript:
    return ProcessScript(returncode=returncode, stdout=stdout.encode(), stderr=stderr.encode())


def test_create_repository_checks_authentication_then_creates(tmp_path: Path) -> None:
    runner = _scripted([_result(), _result()])
    github = GitHubCLI(runner)

    github.create_repository(
        "vibesys-playground/trial",
        visibility="internal",
        source=tmp_path,
    )

    assert _calls(runner) == [
        (["gh", "auth", "status", "--hostname", "github.com"], None),
        (
            [
                "gh",
                "repo",
                "create",
                "vibesys-playground/trial",
                "--internal",
                "--source",
                str(tmp_path),
                "--remote",
                "origin",
            ],
            tmp_path,
        ),
    ]


def test_current_user_checks_authentication_then_reads_login() -> None:
    runner = _scripted([_result(), _result(stdout="octocat\n")])

    assert GitHubCLI(runner).current_user() == "octocat"
    assert _calls(runner) == [
        (["gh", "auth", "status", "--hostname", "github.com"], None),
        (["gh", "api", "user", "--jq", ".login"], None),
    ]


def test_current_user_rejects_empty_login() -> None:
    runner = _scripted([_result(), _result(stdout="\n")])

    with pytest.raises(GitHubCLIError, match="empty authenticated user"):
        GitHubCLI(runner).current_user()


def test_clone_repository_reports_unauthenticated_user(tmp_path: Path) -> None:
    runner = _scripted([_result(1, stderr="not logged into any GitHub hosts")])

    with pytest.raises(GitHubAuthenticationError, match=r"gh auth login.*not logged"):
        GitHubCLI(runner).clone_repository("owner/trial", tmp_path / "trial")

    assert len(_calls(runner)) == 1


def test_repository_error_includes_gh_detail(tmp_path: Path) -> None:
    runner = _scripted([_result(), _result(1, stderr="name already exists")])

    with pytest.raises(GitHubCLIError, match="name already exists"):
        GitHubCLI(runner).clone_repository("owner/trial", tmp_path / "trial")


def test_missing_gh_has_install_guidance() -> None:
    def missing(_spec: ProcessSpec) -> Never:
        raise FileNotFoundError("gh")

    with pytest.raises(GitHubCLIUnavailableError, match=r"https://cli\.github\.com"):
        GitHubCLI(FakeProcesses(missing, pass_time=ManualClock().advance)).ensure_authenticated()


def test_issue_operations_use_explicit_repository_and_preserve_json() -> None:
    runner = _scripted(
        [
            _result(stdout='[[{"number": 8}]]'),
            _result(stdout='{"number": 8}'),
            _result(stdout="https://github.com/owner/repo/issues/9\n"),
            _result(),
            _result(),
            _result(),
            _result(),
        ]
    )
    github = GitHubCLI(runner)

    assert github.list_issues("owner/repo") == [
        {
            "number": 8,
            "title": None,
            "body": None,
            "state": None,
            "labels": [],
            "createdAt": None,
            "updatedAt": None,
            "author": None,
            "url": None,
        }
    ]
    assert github.view_issue("owner/repo", 8) == {"number": 8}
    assert (
        github.create_issue("owner/repo", title="title", body="body", labels=["vibesys:type/bug"])
        == 9
    )
    github.edit_issue("owner/repo", 8, add_labels=["vibesys:status/blocked"])
    github.comment_issue("owner/repo", 8, "metadata")
    github.set_issue_state("owner/repo", 8, issue_open=False)
    github.ensure_label("owner/repo", "vibesys:type/bug")

    assert [call[0][1:4] for call in _calls(runner)] == [
        ["api", "--paginate", "--slurp"],
        ["issue", "view", "8"],
        ["issue", "create", "--repo"],
        ["issue", "edit", "8"],
        ["issue", "comment", "8"],
        ["issue", "close", "8"],
        ["label", "create", "vibesys:type/bug"],
    ]
    assert "repos/owner/repo/issues?state=all&per_page=100" in _calls(runner)[0][0]


@pytest.mark.parametrize(
    "stdout",
    ["[" * 100_000, "[" * 100_000 + "]" * 100_000, "not json", '{"number": 8'],
    ids=["deep-open", "deep-closed", "not-json", "truncated"],
)
def test_unparseable_issue_json_is_a_cli_error(stdout: str) -> None:
    github = GitHubCLI(_scripted([_result(stdout=stdout)]))

    with pytest.raises(GitHubCLIError, match="invalid JSON for issue view"):
        github.view_issue("owner/repo", 8)
