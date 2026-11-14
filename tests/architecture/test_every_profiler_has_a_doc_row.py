"""Every value `--profiler` accepts is documented in the CLI guide, in both places it lists them.

The guide's "Mental Model" table names the values in its Profiler row, and its "Profiler" section
gives each value a row. Both are prose; this check holds them to the parser's choices.
"""

import re
from pathlib import Path

from vibesys.api.profilers import CLI_PROFILER_CHOICES

_GUIDE = Path(__file__).resolve().parents[2] / "docs" / "cli-flags.md"
_CHOICES = sorted(kind.value for kind in CLI_PROFILER_CHOICES)


def _text() -> str:
    return _GUIDE.read_text(encoding="utf-8")


def test_the_mental_model_profiler_row_names_every_choice() -> None:
    [row] = re.findall(r"^\| Profiler \| `--profiler` \|(.*)\|$", _text(), re.MULTILINE)

    assert sorted(re.findall(r"`([a-z_]+)`", row)) == _CHOICES


def test_the_profiler_section_has_one_row_per_choice() -> None:
    text = _text()
    start = text.index("\n## Profiler\n")
    end = text.index("\n## ", start + 1)

    rows = re.findall(r"^\| `([a-z_]+)` +\|", text[start:end], re.MULTILINE)

    assert sorted(rows) == _CHOICES
