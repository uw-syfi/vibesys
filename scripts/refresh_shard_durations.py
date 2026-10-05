#!/usr/bin/env python3
"""Rewrite tests/support/shard_durations.json from the CI shards' measured seconds.

Every test shard uploads a ``shard-durations-I`` artifact. Download one run's
artifacts (a green run on main is the usual source) and merge them:

    gh run download RUN_ID --pattern 'shard-durations-*' --dir /tmp/durations
    uv run python scripts/refresh_shard_durations.py /tmp/durations
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tests.support.sharding import DEFAULT_DURATIONS, merge_durations


def main(argv: list[str]) -> int:
    """Merge every ``shard-durations-*.json`` under the given directory."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path, help="where the shard artifacts were downloaded")
    args = parser.parse_args(argv)
    files = sorted(args.directory.rglob("*.json"))
    if not files:
        sys.stderr.write(f"no shard duration files under {args.directory}\n")
        return 1
    merged = merge_durations(json.loads(path.read_text()) for path in files)
    DEFAULT_DURATIONS.write_text(json.dumps(merged, indent=1) + "\n", encoding="utf-8")
    sys.stdout.write(f"merged {len(files)} shard records, {len(merged)} test files\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
