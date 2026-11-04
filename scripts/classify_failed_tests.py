#!/usr/bin/env python3
"""Say, for each test a merge-queue run failed, whether it also fails on main.

When a `merge_group` run of the `test` job fails, the queue ejects the pull request. A test that
fails on main as well (flaky, or main is broken) is not this pull request's doing, and the author
needs to know that to avoid chasing it. The `classify-failures` job of `.github/workflows/test.yml`
runs this script on a checkout of the base the queue branched from:

* each failed shard uploaded pytest's `lastfailed` cache (a JSON object of node ids that failed);
* `run` reruns exactly those node ids once on this checkout (main's tree) and writes a job
  summary that labels every one `FAILS_ON_MAIN` or `FAILS_ONLY_WITH_THE_PR`.

The pull request's own result is never retried and never changed: this job only explains it.

Usage:
    uv run python scripts/classify_failed_tests.py run failed-tests/*/lastfailed
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
#: Past this many failed tests a rerun is not an explanation; the run is plainly broken.
MAX_RERUN = 200


class Outcome(StrEnum):
    """Where a failed test also fails."""

    FAILS_ON_MAIN = "fails on main (flaky or broken main)"
    FAILS_ONLY_WITH_THE_PR = "fails only with this PR"


def failed_ids(lastfailed_texts: Iterable[str]) -> list[str]:
    """Node ids that failed, from pytest `lastfailed` caches: sorted, each once."""
    ids: set[str] = set()
    for text in lastfailed_texts:
        loaded = json.loads(text)
        if not isinstance(loaded, dict):
            message = "a lastfailed cache is a JSON object of node ids"
            raise TypeError(message)
        ids.update(node for node, failed in loaded.items() if failed is True)
    return sorted(ids)


def classify(failed_with_pr: Sequence[str], failed_on_main: Iterable[str]) -> dict[str, Outcome]:
    """Label each test that failed with the pull request by whether it failed on main too."""
    on_main = set(failed_on_main)
    return {
        node: Outcome.FAILS_ON_MAIN if node in on_main else Outcome.FAILS_ONLY_WITH_THE_PR
        for node in failed_with_pr
    }


def summary(outcomes: Mapping[str, Outcome], *, base: str, skipped: int = 0) -> str:
    """The markdown the job writes: one section per outcome, then a note on what was not rerun."""
    lines = [f"## Failed tests of this queue run, rerun once on main (`{base}`)", ""]
    for outcome in Outcome:
        nodes = [node for node, label in outcomes.items() if label is outcome]
        lines.append(f"### {outcome.value}: {len(nodes)}")
        lines.extend(f"- `{node}`" for node in nodes)
        lines.append("")
    if skipped:
        lines.append(
            f"{skipped} more failed tests were not rerun (more than {MAX_RERUN} failures is a broken run, not a flake)."
        )
    lines.append("The pull request's own result is unchanged: this only explains it.")
    return "\n".join(lines) + "\n"


def _run_on_main(ids: Sequence[str]) -> list[str]:
    """Rerun `ids` once on this checkout and return the ones that fail."""
    cache = REPO_ROOT / ".pytest_cache"
    shutil.rmtree(cache, ignore_errors=True)
    # lint-waiver: LW-164201 [S603]; Runs this interpreter's pytest on node ids read from CI's own shard artifacts, with no shell.
    subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "-q", "--no-cov", *ids],
        cwd=REPO_ROOT,
        check=False,
    )
    last = cache / "v" / "cache" / "lastfailed"
    return failed_ids([last.read_text(encoding="utf-8")]) if last.is_file() else []


def _write_summary(text: str) -> None:
    print(text)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with Path(target).open("a", encoding="utf-8") as out:
            out.write(text)


def main(argv: list[str] | None = None) -> int:
    """Rerun the failed tests on this checkout and write the labelled summary; always exits 0."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("run",))
    parser.add_argument("lastfailed", nargs="+", type=Path)
    args = parser.parse_args(argv)
    ids = failed_ids(path.read_text(encoding="utf-8") for path in args.lastfailed if path.is_file())
    base = os.environ.get("BASE_SHA", "main")
    if not ids:
        _write_summary("No failed test ids were recorded (the failure was not a test).\n")
        return 0
    rerun, skipped = ids[:MAX_RERUN], max(0, len(ids) - MAX_RERUN)
    outcomes = classify(rerun, _run_on_main(rerun))
    _write_summary(summary(outcomes, base=base, skipped=skipped))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
