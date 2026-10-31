"""Which committed native goldens a change to the schemas or to a golden input made stale.

The native goldens (``clients/libs/run-fixtures/src/native`` and
``clients/apps/desktop-shell/e2e/scenarios``) embed the API and store schema hashes in their first
record and are fingerprinted over the ``.proto`` files and the code that projects them. A change to
a ``.proto`` file therefore invalidates all of them, and the golden tests only say so after they
project every scenario. This module answers from hashes alone, in well under a second, so the
proto check group can fail a stale golden before any test projects one.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from tests.support.wire_goldens import REGENERATE

from vs_proto.api import ClientReplay, Record

if TYPE_CHECKING:
    from pathlib import Path

#: Stale goldens named in the report; the count says how many more there are.
_NAMED = 10


def stale_goldens(
    directory: Path, *, label: str, fingerprint: str, api_schema_hash: str, store_schema_hash: str
) -> list[str]:
    """One line per way the goldens in ``directory`` differ from the inputs they derive from.

    ``label`` is the directory as the report names it. ``fingerprint`` is the hash the golden
    inputs have now, which ``source.sha256`` must record. Every ``.pb`` whose first record opens
    an epoch must embed the schema hashes passed in, and ``goldens.sha256`` must list the digest
    of every ``.pb``.
    """
    problems = []
    recorded = (directory / "source.sha256").read_text().strip()
    if recorded != fingerprint:
        problems.append(
            f"{label}/source.sha256 records golden inputs {recorded[:12]}, "
            f"which now hash to {fingerprint[:12]}"
        )
    goldens = sorted(directory.glob("*.pb"))
    listed = dict(
        line.split(" ") for line in (directory / "goldens.sha256").read_text().splitlines()
    )
    for path in goldens:
        wire = path.read_bytes()
        if listed.pop(path.name, None) != hashlib.sha256(wire).hexdigest():
            problems.append(f"{label}/{path.name} is not the bytes goldens.sha256 lists")
        problems += _stale_hashes(f"{label}/{path.name}", wire, api_schema_hash, store_schema_hash)
    problems += [f"{label}/goldens.sha256 lists {name}, which is gone" for name in sorted(listed)]
    return problems


def report_stale(problems: list[str]) -> str:
    """The failure message for ``problems``: the first few, how many more, and the fix."""
    named = "\n".join(f"  {problem}" for problem in problems[:_NAMED])
    more = f"\n  and {len(problems) - _NAMED} more" if len(problems) > _NAMED else ""
    return f"The goldens are stale:\n{named}{more}\nRegenerate and commit them: {REGENERATE}"


def _stale_hashes(
    name: str, wire: bytes, api_schema_hash: str, store_schema_hash: str
) -> list[str]:
    try:
        replay = ClientReplay.from_binary(wire)
    except ValueError:
        return [f"{name} does not decode as a replay"]
    if not replay.batches or not replay.batches[0].records:
        return []
    opening = Record.from_binary(replay.batches[0].records[0]).body
    if opening is None or opening.field != "epoch_started":
        return []
    return [
        f"{name} embeds {kind} schema hash {embedded[:12]}, the current one is {current[:12]}"
        for kind, embedded, current in (
            ("API", opening.value.api_schema_hash, api_schema_hash),
            ("store", opening.value.store_schema_hash, store_schema_hash),
        )
        if embedded != current
    ]
