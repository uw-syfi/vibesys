"""Command line for ``python3 -m scripts.published_skills``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from scripts.published_skills.manifest import (
    MANIFEST_PATH,
    ManifestError,
    check_manifest,
    render_manifest,
)
from scripts.published_skills.skills import check_skills

REPOSITORY = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    """Check the published surface, or with ``--write`` rewrite the manifest's skill list."""
    parser = argparse.ArgumentParser(
        prog="python3 -m scripts.published_skills", description=__doc__
    )
    parser.add_argument(
        "--write", action="store_true", help="rewrite the manifest's skill list from the tree"
    )
    args = parser.parse_args(argv)
    if args.write:
        try:
            text = render_manifest(REPOSITORY)
        except ManifestError as exc:
            sys.stderr.write(f"{exc}\n")
            return 1
        (REPOSITORY / MANIFEST_PATH).write_text(text, encoding="utf-8")
        return 0
    findings = [*check_manifest(REPOSITORY), *map(str, check_skills(REPOSITORY))]
    for finding in findings:
        sys.stdout.write(f"{finding}\n")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
