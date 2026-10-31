"""The native and wire goldens match the schemas and inputs they were made from, from hashes alone.

The proto check group runs this file, so a change to a ``.proto`` file that leaves the goldens
stale fails there in seconds, before the golden tests project every scenario. Each failure names
the one command that regenerates them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.support.journey_fixtures import ROOT
from tests.support.journey_fixtures import fingerprint as journey_fingerprint
from tests.support.native_goldens import report_stale, stale_goldens
from tests.support.shared_scenarios import GOLDENS, REPOSITORY
from tests.support.shared_scenarios import fingerprint as shared_fingerprint
from tests.support.wire_goldens import stale_wire_goldens

from vs_proto.api import API_SCHEMA_HASH, STORE_SCHEMA_HASH

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


@pytest.mark.parametrize(
    ("directory", "fingerprint"),
    [(GOLDENS, shared_fingerprint), (ROOT, journey_fingerprint)],
    ids=["run-fixtures", "desktop-shell"],
)
def test_native_goldens_match_the_schemas_and_inputs_they_were_made_from(
    directory: Path, fingerprint: Callable[[], str]
) -> None:
    stale = stale_goldens(
        directory,
        label=directory.relative_to(REPOSITORY).as_posix(),
        fingerprint=fingerprint(),
        api_schema_hash=API_SCHEMA_HASH,
        store_schema_hash=STORE_SCHEMA_HASH,
    )
    if stale:
        pytest.fail(report_stale(stale), pytrace=False)


def test_wire_goldens_match_the_inputs_they_were_made_from() -> None:
    stale = stale_wire_goldens()
    if stale:
        pytest.fail(report_stale(stale), pytrace=False)
