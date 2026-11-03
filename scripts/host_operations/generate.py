"""Generate admission contracts and subscription policies from descriptors."""

from __future__ import annotations

import sys
from pathlib import Path

from scripts.host_operations.api import compile_outputs


def generate(root: Path, image: bytes, *, include_schema: bool = True) -> None:
    """Publish projections of the descriptor contracts used by the run path and gateway."""
    for name, content in compile_outputs(image, None).items():
        if name.endswith(".proto") and not include_schema:
            continue
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    generate(
        Path(__file__).resolve().parents[2],
        Path(sys.argv[1]).read_bytes(),
        include_schema="--bindings" not in sys.argv,
    )
