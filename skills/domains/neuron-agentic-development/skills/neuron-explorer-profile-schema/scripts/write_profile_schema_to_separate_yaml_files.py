#!/usr/bin/env python3
"""Split `neuron-explorer --show-profile-schema` output into per-table YAML files.

`neuron-explorer --show-profile-schema` (with parquet output format) prints all
schema YAML files concatenated and separated by `---`. This script reads that
combined stream from stdin and writes one file per table, named by the
top-level YAML key (e.g. `Instruction:` -> `Instruction.yaml`).

The OpenAPI root document (starts with `openapi:`) is written as `schema.yaml`.

Usage:
    neuron-explorer --show-profile-schema | \\
        write_profile_schema_to_separate_yaml_files.py [--out DIR]

Default output directory is ./schema/ relative to the current working dir.

Notes:
    - Keep the output directory in sync with the version of neuron-explorer
      you actually run. Stale schema files cause subtle field/type mismatches
      during profile analysis.
    - Comments in the upstream YAML are preserved (the script does not
      reformat); each chunk is written verbatim.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# First non-comment, non-blank line starting with `<key>:` at column 0.
_TOP_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:", re.MULTILINE)


def _top_level_key(chunk: str) -> str | None:
    """Return the first top-level YAML key in `chunk`, or None."""
    for line in chunk.splitlines():
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#"):
            continue
        # Top-level keys have no leading whitespace.
        if line.startswith(" ") or line.startswith("\t"):
            return None
        m = _TOP_KEY_RE.match(line)
        if m:
            return m.group(1)
        return None
    return None


def split_schema(blob: str) -> list[tuple[str, str]]:
    """Split the combined schema stream into (filename, content) pairs."""
    # `neuron-explorer --show-profile-schema` joins per-table YAML files with
    # a literal `---` separator and no surrounding whitespace. Some upstream
    # files don't end with a newline, so the separator may not be on its own
    # line. Split on any `---` and detect each chunk's top-level key. The
    # top-level key (or `openapi:` for the root) determines the output
    # filename.
    chunks = re.split(r"---", blob)
    out: list[tuple[str, str]] = []
    for raw in chunks:
        chunk = raw.strip("\n")
        if not chunk.strip():
            continue
        key = _top_level_key(chunk)
        if key is None:
            print(
                f"warning: could not detect top-level key in a chunk; skipping. "
                f"first 80 chars: {chunk[:80]!r}",
                file=sys.stderr,
            )
            continue
        # The OpenAPI root document uses `openapi:` as its first key.
        filename = "schema.yaml" if key == "openapi" else f"{key}.yaml"
        out.append((filename, chunk + "\n"))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default="./schema",
        help="Output directory (default: ./schema)",
    )
    parser.add_argument(
        "--input",
        default="-",
        help="Input file (default: stdin)",
    )
    args = parser.parse_args()

    if args.input == "-":
        blob = sys.stdin.read()
    else:
        blob = Path(args.input).read_text()

    if not blob.strip():
        print("error: no input received on stdin", file=sys.stderr)
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    pairs = split_schema(blob)
    if not pairs:
        print("error: no YAML chunks parsed from input", file=sys.stderr)
        return 1

    for filename, content in pairs:
        path = out_dir / filename
        path.write_text(content)

    print(f"wrote {len(pairs)} files to {out_dir}/", file=sys.stderr)
    for filename, _ in sorted(pairs):
        print(f"  {filename}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
