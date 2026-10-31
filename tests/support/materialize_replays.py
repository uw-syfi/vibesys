"""Generate the derived native replays and print the directory that holds them.

``python -m tests.support.materialize_replays`` is the one command that turns the declarations into
the replays the clients read. It stores the set under ``tests.support.derived_replays.key`` and
prints that directory. It loads the product, so a caller asks ``python -m
tests.support.derived_replays path`` first and runs this only when that exits 1.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from tests.support import derived_replays, journey_fixtures, shared_scenarios

if TYPE_CHECKING:
    from pathlib import Path


def generate(directory: Path) -> None:
    """Project every declaration of both sets into ``directory``."""
    (directory / "shared").mkdir()
    for path in shared_scenarios.declarations():
        replay = shared_scenarios.project_shared(path)
        (directory / "shared" / f"{path.stem}.pb").write_bytes(replay.to_binary())
        _progress(f"shared {path.stem}")
    (directory / "journeys").mkdir()
    for path in sorted(journey_fixtures.ROOT.glob("*.json")):
        replay = journey_fixtures.project_scenario(path.stem)
        (directory / "journeys" / f"{path.stem}.pb").write_bytes(replay.to_binary())
        _progress(f"journeys {path.stem}")
        for name, variant in journey_fixtures.scenario_variants(path.stem).items():
            (directory / "journeys" / f"{name}.pb").write_bytes(variant.to_binary())
    for name, payload in journey_fixtures.live_artifacts().items():
        (directory / "journeys" / name).write_bytes(payload)


def _progress(line: str) -> None:
    sys.stderr.write(f"derived replays: {line}\n")
    sys.stderr.flush()


def main() -> int:
    """Make the set the current inputs name, if the cache lacks it, and print its directory."""
    directory = derived_replays.materialize(
        cache=derived_replays.cache_directory(),
        set_key=derived_replays.key(),
        produce=generate,
    )
    sys.stdout.write(f"{directory}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
