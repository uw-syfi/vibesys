"""Backend selection is strict and immutable when resuming a plain run."""

import pytest
from pydantic import ValidationError

from vibesys.errors import ConfigurationError
from vibesys.loops.issue_queue.orchestration import (
    IssueQueueOptions,
    compare_resume,
    descriptor_from_options,
)
from vs_issue_tracker.api import IssueTrackerConfig


def _options(**overrides: object) -> IssueQueueOptions:
    values: dict[str, object] = {
        "max_rounds": 3,
        "max_attempts_per_issue": 2,
        "max_issues_per_perf_eval": 1,
    }
    values.update(overrides)
    return IssueQueueOptions.model_validate(values)


def test_github_requires_owner_and_repository() -> None:
    with pytest.raises(ValidationError, match="repository is required"):
        IssueTrackerConfig.from_backend("github")
    with pytest.raises(ValidationError, match="OWNER/REPOSITORY"):
        IssueTrackerConfig.from_backend("github", repository="repo-only")
    with pytest.raises(ValidationError, match="only valid"):
        IssueTrackerConfig(repository="owner/repo")


def test_resume_rejects_a_tracker_backend_or_repository_change() -> None:
    original = descriptor_from_options(
        _options(tracker=IssueTrackerConfig.from_backend("github", repository="owner/repo"))
    )
    changed = descriptor_from_options(
        _options(tracker=IssueTrackerConfig.from_backend("github", repository="owner/other"))
    )

    with pytest.raises(ConfigurationError, match="tracker"):
        compare_resume(original, changed)
