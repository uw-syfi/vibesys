"""Regenerate the coverage.py glob oracle table that repoctl's matcher is tested against.

repoctl reimplements coverage.py's include and omit glob matching in
`support/repoctl/execution/python/coverage_scope.go`. This script asks
coverage.py itself whether each pattern matches each repository-relative path
and commits the answers, so the Go test never runs coverage.py.

Run `uv run python scripts/generate_coverage_glob_cases.py` after changing the
pattern or path lists or upgrading coverage.
"""

from __future__ import annotations

import json
import os
import tempfile
from itertools import product
from pathlib import Path

import coverage
from coverage.exceptions import ConfigError
from coverage.files import GlobMatcher, prep_patterns

OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "support/repoctl/execution/python/testdata/coverage_globs.json"
)
ROOT_PLACEHOLDER = "{root}"

PATTERNS = [
    "src",
    "src/*",
    "src/**",
    "src/vibesys/*.py",
    "src/vibesys/",
    "tests",
    "tests/**/test_*.py",
    "libs/vs-core/src",
    "*.py",
    "__main__.py",
    "*/__main__.py",
    "**/__main__.py",
    "*/vs_proto/_generated/*",
    "scripts/corevalue/tests/*",
    "*/tests/*",
    "**/tests/*.py",
    "tests/test_?.py",
    "tests/test_[ab].py",
    "tests/test_[a-c]*.py",
    "src/a.b/*.py",
    "src/a+b/*",
    f"{ROOT_PLACEHOLDER}/src/*",
    f"{ROOT_PLACEHOLDER}/*.py",
    "src/vibesys\\*.py",
    "x**",
    "**x",
    "a***",
    "a/**/**",
    "src/[",
    "src/]",
]

PATHS = [
    "main.py",
    "__main__.py",
    "src/__main__.py",
    "src/vibesys/__main__.py",
    "src/main.py",
    "src/vibesys/run.py",
    "src/vibesys/sub/run.py",
    "src/vibesys/run.txt",
    "src/vibesys",
    "srcs/vibesys/run.py",
    "src/a.b/x.py",
    "src/aXb/x.py",
    "src/a+b/x.py",
    "src/aab/x.py",
    "tests/test_a.py",
    "tests/test_b.py",
    "tests/test_d.py",
    "tests/test_ab.py",
    "tests/test_cat.py",
    "tests/unit/test_x.py",
    "tests/unit/deep/test_x.py",
    "tests/helper.py",
    "tests/unit/helper.py",
    "libs/vs-core/src/vs_core/api.py",
    "libs/vs-core/tests/test_api.py",
    "libs/vs-proto/src/vs_proto/_generated/x_pb2.py",
    "libs/vs-proto/src/vs_proto/_generated/sub/x_pb2.py",
    "libs/vs-proto/src/vs_proto/x.py",
    "scripts/corevalue/tests/test_a.py",
    "scripts/corevalue/tests/sub/test_a.py",
    "scripts/corevalue/run.py",
]


def _matches(pattern: str, path: str, root: str) -> bool | None:
    """Whether coverage.py includes `path`, or None when it rejects `pattern`."""
    try:
        matcher = GlobMatcher(prep_patterns([pattern.replace(ROOT_PLACEHOLDER, root)]))
    except ConfigError:
        return None
    return matcher.match(f"{root}/{path}")


def main() -> None:
    """Write the table."""
    with tempfile.TemporaryDirectory() as directory:
        root = os.path.realpath(directory)
        os.chdir(root)
        cases = [
            {"pattern": pattern, "path": path, "match": _matches(pattern, path, root)}
            for pattern, path in product(PATTERNS, PATHS)
        ]
    document = {"coverage_version": coverage.__version__, "cases": cases}
    OUTPUT.write_text(json.dumps(document, indent=1) + "\n")


if __name__ == "__main__":
    main()
