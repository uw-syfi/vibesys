from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tests.entrypoints.web_home.support import Home


def test_notes_round_trip_in_the_tui_file_format(home: Home) -> None:
    assert home.get("/api/notes/run-1").json() == {"note": None}

    saved = home.put("/api/notes/run-1", {"text": "check the p99"}).json()["note"]

    path = home.config.state_home / "tui" / "notes" / "run-1.json"
    assert json.loads(path.read_text()) == {
        "runId": "run-1",
        "text": "check the p99",
        "createdAt": "2026-09-28T12:00:00.000Z",
        "updatedAt": "2026-09-28T12:00:00.000Z",
    }
    assert saved == json.loads(path.read_text())
    assert home.get("/api/notes/run-1").json()["note"] == saved


def test_a_note_written_by_the_tui_is_read_and_its_creation_time_kept(home: Home) -> None:
    path = home.config.state_home / "tui" / "notes" / "run-2.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "runId": "run-2",
                "text": "old",
                "createdAt": "2026-01-01T00:00:00.000Z",
                "updatedAt": "2026-01-01T00:00:00.000Z",
            }
        )
    )

    saved = home.put("/api/notes/run-2", {"text": "new"}).json()["note"]

    assert (saved["createdAt"], saved["updatedAt"], saved["text"]) == (
        "2026-01-01T00:00:00.000Z",
        "2026-09-28T12:00:00.000Z",
        "new",
    )


def test_run_ids_map_to_the_same_file_name_as_the_tui(home: Home) -> None:
    home.put("/api/notes/a%2Fb%20c%F0%9F%98%80", {"text": "x"})

    names = sorted(p.name for p in (home.config.state_home / "tui" / "notes").iterdir())
    assert names == ["a_b_c__.json"]


def test_a_corrupt_note_reads_as_absent(home: Home) -> None:
    path = home.config.state_home / "tui" / "notes" / "run-3.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")

    assert home.get("/api/notes/run-3").json() == {"note": None}
