"""Write the generated display-name catalog for clients."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

from vibesys.api.display_catalog import display_names_typescript

if TYPE_CHECKING:
    from collections.abc import Sequence


def main(arguments: Sequence[str]) -> None:
    """Write the generated module to the path named by the one argument."""
    if len(arguments) != 1:
        message = "usage: python scripts/generate_display_names.py OUTPUT"
        raise SystemExit(message)
    output = Path(arguments[0])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(display_names_typescript())


if __name__ == "__main__":
    main(sys.argv[1:])
