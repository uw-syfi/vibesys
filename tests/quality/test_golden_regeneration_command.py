"""Every stale-golden failure names the one command that regenerates the goldens.

``./scripts/regenerate_goldens.sh`` rewrites the conversion goldens, the Skeleton wire golden and
the retained gateway replies. Agents used to rediscover a hand-run chain of commands, so no failure
message, doc or skill may name a piece of that chain as the fix.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from tests.support.golden_regeneration import REGENERATE

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/regenerate_goldens.sh"
#: Files that fail a test over a stale golden; each message must carry the command. A Go test
#: cannot import the Python constant, so it spells the script out.
GO_FAILURE_SITES = (
    "gateway/internal/server/writer_fixtures_test.go",
    "gateway/internal/server/retained_writers_test.go",
)
PYTHON_FAILURE_SITES = (
    "tests/vibesys/run/test_runlog_conversion.py",
    "tests/vibesys/run/test_journey_fixtures.py",
    "tests/vibesys/run/test_projection_shapes.py",
)
#: The pieces of the old chain, which only the script and the generators themselves may name.
CHAIN = (
    "python -m tests.support.shared_scenarios",
    "python proto/testdata/conversion/generate.py",
    "VIBESYS_UPDATE_GOLDEN=1 go test",
)
GUIDANCE = (".agents", "docs", "clients/apps/desktop-shell/e2e/tools", "proto/testdata")


def test_the_regeneration_command_is_an_executable_script() -> None:
    assert f"./{SCRIPT.relative_to(ROOT)}" == REGENERATE
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK), f"chmod +x {SCRIPT.relative_to(ROOT)}"


@pytest.mark.parametrize("site", GO_FAILURE_SITES)
def test_a_stale_golden_failure_in_go_names_the_regeneration_command(site: str) -> None:
    assert REGENERATE in (ROOT / site).read_text(), f"{site} must name {REGENERATE} as the fix"


@pytest.mark.parametrize("site", PYTHON_FAILURE_SITES)
def test_a_stale_golden_failure_in_python_formats_the_regeneration_command(site: str) -> None:
    text = (ROOT / site).read_text()

    assert "from tests.support.golden_regeneration import REGENERATE" in text, site
    assert "{REGENERATE}" in text, f"{site} must name {REGENERATE} as the fix"


def test_docs_and_skills_do_not_send_a_reader_down_the_hand_run_chain() -> None:
    offenders = [
        f"{path.relative_to(ROOT)} names `{piece}`"
        for directory in GUIDANCE
        for path in sorted((ROOT / directory).rglob("*"))
        if path.suffix in {".md", ".ts"} and path.is_file()
        for piece in CHAIN
        if piece in path.read_text()
    ]

    assert not offenders, f"name {REGENERATE} instead:\n" + "\n".join(offenders)
