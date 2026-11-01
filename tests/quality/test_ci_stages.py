"""The two CI stages cannot lose a check: whatever skips a pull request still runs in the merge queue.

`.github/workflows/test.yml` runs a fast commit stage on `pull_request` and the full suite on
`merge_group` and `push`. A job that carries `github.event_name != 'pull_request'` belongs to the
full stage only. Two mistakes would silently weaken what is checked before a merge: a job that runs
on pull requests only (it would be skipped by the queue, so the merge never sees it), and a job that
`Required PR CI` does not wait for (its failure would not block). Both are checked over the parsed
workflow, with one violating and one compliant synthetic workflow each.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "test.yml"
#: The only job that may run on pull requests and nowhere else: the impact-selected tests, which the
#: queue's full Python shards cover completely.
PULL_REQUEST_ONLY = {"test-impact"}
#: Jobs `Required PR CI` is not itself.
AGGREGATORS = {"required-pr-ci", "ci-budget"}


def _condition(job: dict[str, Any]) -> str:
    return " ".join(str(job.get("if", "")).split())


def pull_request_only(jobs: dict[str, dict[str, Any]]) -> set[str]:
    """Jobs whose condition requires the event to be a pull request."""
    return {
        name
        for name, job in jobs.items()
        if _condition(job).startswith("github.event_name == 'pull_request'")
    }


def unwatched(jobs: dict[str, dict[str, Any]]) -> set[str]:
    """Jobs the Required PR CI aggregator does not list in `needs`."""
    needed = set(jobs["required-pr-ci"]["needs"])
    return set(jobs) - needed - AGGREGATORS


def _jobs(text: str) -> dict[str, dict[str, Any]]:
    return yaml.safe_load(text)["jobs"]


_COMPLIANT = """
jobs:
  changes: {}
  fast: {needs: changes}
  full: {needs: changes, if: "github.event_name != 'pull_request' && true"}
  impact: {needs: changes, if: "github.event_name == 'pull_request' && true"}
  required-pr-ci: {needs: [changes, fast, full, impact]}
"""
_PR_ONLY_NOT_ALLOWED = _COMPLIANT.replace(
    "fast: {needs: changes}", "fast: {needs: changes, if: \"github.event_name == 'pull_request'\"}"
)
_UNWATCHED = _COMPLIANT.replace("[changes, fast, full, impact]", "[changes, fast, impact]")


def test_a_pull_request_only_job_is_found_by_its_condition() -> None:
    assert pull_request_only(_jobs(_COMPLIANT)) == {"impact"}
    assert pull_request_only(_jobs(_PR_ONLY_NOT_ALLOWED)) == {"fast", "impact"}


def test_a_job_the_aggregator_does_not_wait_for_is_found() -> None:
    assert unwatched(_jobs(_COMPLIANT)) == set()
    assert unwatched(_jobs(_UNWATCHED)) == {"full"}


def test_no_job_runs_on_pull_requests_and_nowhere_else_but_the_impact_tests() -> None:
    jobs = _jobs(WORKFLOW.read_text(encoding="utf-8"))
    extra = pull_request_only(jobs) - PULL_REQUEST_ONLY
    assert not extra, (
        f"{sorted(extra)} run only on pull requests, so the merge queue would never run them. "
        "Drop the pull_request-only condition, or add the job to PULL_REQUEST_ONLY with the reason "
        "the queue's full stage already covers it."
    )
    assert set(jobs) >= PULL_REQUEST_ONLY


def test_required_pr_ci_waits_for_every_job() -> None:
    jobs = _jobs(WORKFLOW.read_text(encoding="utf-8"))
    missing = unwatched(jobs)
    assert not missing, (
        f"{sorted(missing)} are not in Required PR CI's `needs`, so a failure there would not block a "
        "merge. Add each job to `required-pr-ci.needs`."
    )
