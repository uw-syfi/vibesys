"""Every registered domain has a row in each documented table of domains.

The user guide and the contributor guide each list the domains an input bundle can name in
`[agent].domain`. The tables are prose; this check holds them complete against `DomainName`.
"""

import re
from pathlib import Path

import pytest

from vibesys.constants import DomainName

_DOCS = Path(__file__).resolve().parents[2] / "docs"
_ROW = re.compile(r"^\| `([a-z-]+)` +\|", re.MULTILINE)


def _section(path: Path, heading: str) -> str:
    text = path.read_text(encoding="utf-8")
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start : end if end != -1 else len(text)]


@pytest.mark.parametrize(
    ("doc", "heading"),
    [
        ("cli-flags.md", "## Domain and Modality"),
        ("contributing/domains.md", "# Domains: pointing vibesys at your problem space"),
    ],
)
def test_every_domain_has_a_row_in_the_table_of_domains(doc: str, heading: str) -> None:
    rows = _ROW.findall(_section(_DOCS / doc, heading))

    assert sorted(rows) == sorted(domain.value for domain in DomainName)
