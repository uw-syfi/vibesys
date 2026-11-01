#!/usr/bin/env python3
"""Rewrite tests/support/shard_durations.json from the CI shards' measured seconds.

Every test shard uploads a ``shard-durations-I`` artifact. Download one run's
artifacts (a green run on main is the usual source) and merge them:

    gh run download RUN_ID --pattern 'shard-durations-*' --dir /tmp/durations
    uv run python -m scripts.refresh_shard_durations /tmp/durations

CI runs it with ``--output`` on every push to main to build the record that later
runs read (see ``.github/workflows/test.yml``); without it, the checked-in
cold-start record is rewritten.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tests.support.sharding import (
    DEFAULT_DURATIONS,
    SMOOTHING_WEIGHT,
    merge_durations,
    parse_durations,
    smooth_durations,
)


def main(argv: list[str]) -> int:
    """Merge every ``shard-durations-*.json`` under the given directory."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path, help="where the shard artifacts were downloaded")
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_DURATIONS, help="where to write the merged record"
    )
    parser.add_argument(
        "--previous",
        type=Path,
        default=None,
        help=(
            "the record the run started from; each measured file then moves "
            f"{SMOOTHING_WEIGHT:g} of the way from it to its seconds in this run instead of "
            "replacing it"
        ),
    )
    args = parser.parse_args(argv)
    files = sorted(args.directory.rglob("*.json"))
    if not files:
        sys.stderr.write(f"no shard duration files under {args.directory}\n")
        return 1
    merged = merge_durations(json.loads(path.read_text()) for path in files)
    previous = _previous_record(args.previous)
    if previous is not None:
        merged = smooth_durations(previous, merged)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(merged, indent=1) + "\n", encoding="utf-8")
    sys.stdout.write(f"merged {len(files)} shard records, {len(merged)} test files\n")
    return 0


def _previous_record(path: Path | None) -> dict[str, float] | None:
    """The record to smooth into, or ``None`` when there is none or it is unusable.

    A missing file is a first run. A malformed one is reported and replaced by this run's
    seconds, as the shards do with a cached record they cannot read: a corrupt entry must not
    keep the record from ever recovering.
    """
    if path is None or not path.exists():
        return None
    try:
        return parse_durations(path.read_text(encoding="utf-8"), str(path))
    except ValueError as error:
        sys.stderr.write(f"ignoring the previous record: {error}\n")
        return None


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
