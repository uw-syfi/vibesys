"""Generate the desktop app's offline example inventory from the example registry."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from entrypoints.example_catalog import REGISTRY_RELATIVE, load_registry

if TYPE_CHECKING:
    from collections.abc import Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT = (
    REPOSITORY_ROOT / "clients/apps/desktop/src/state/new-run/generated/registered-examples.ts"
)


def _quoted(value: str) -> str:
    """Render one TypeScript string using JSON's well-defined escaping."""
    return json.dumps(value, ensure_ascii=False)


def generated_source(checkout: Path = REPOSITORY_ROOT) -> str:
    """Return the deterministic TypeScript projection of ``examples/registry.toml``."""
    registry_path = checkout / REGISTRY_RELATIVE
    source_hash = hashlib.sha256(registry_path.read_bytes()).hexdigest()
    registry = load_registry(checkout)
    categories = sorted({entry.category for entry in registry.example})
    category_union = " | ".join(_quoted(category) for category in categories)
    rows = "\n".join(
        "  {"
        f"id: {_quoted(entry.name)}, category: {_quoted(entry.category)}, "
        f"needsClone: {str(entry.external_repo).lower()}"
        "},"
        for entry in registry.example
    )
    return f"""/**
 * Generated from examples/registry.toml by scripts/generate_desktop_examples.py. Do not edit.
 * source-sha256: {source_hash}
 */

export type RegisteredExampleCategory = {category_union};

export interface RegisteredExample {{
  readonly id: string;
  readonly category: RegisteredExampleCategory;
  readonly needsClone: boolean;
}}

/** Every runnable example registered by this VibeSys checkout, in registry order. */
export const REGISTERED_EXAMPLES = [
{rows}
] as const satisfies readonly RegisteredExample[];
"""


def main(arguments: Sequence[str]) -> None:
    """Write the generated inventory, or check that the committed one is current."""
    check = False
    output = DEFAULT_OUTPUT
    for argument in arguments:
        if argument == "--check":
            check = True
        elif output == DEFAULT_OUTPUT:
            output = Path(argument)
        else:
            message = "usage: python scripts/generate_desktop_examples.py [OUTPUT] [--check]"
            raise SystemExit(message)
    expected = generated_source()
    if check:
        try:
            actual = output.read_text(encoding="utf-8")
        except OSError as exc:
            message = f"cannot read {output}: {exc}"
            raise SystemExit(message) from exc
        if actual != expected:
            message = f"{output} is stale; run `uv run python scripts/generate_desktop_examples.py`"
            raise SystemExit(message)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(expected, encoding="utf-8")


if __name__ == "__main__":
    main(sys.argv[1:])
