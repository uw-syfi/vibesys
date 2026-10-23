"""Historical format -> conversion -> serialized catalog.

The shape table is generated offline. These tests never invoke git. Real flock
writer races belong to the real tiers; no fake lock or writer race is modeled here.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest
from hypothesis import example, given
from hypothesis import strategies as st
from tests.server.host_helper_support import fake_host

from server.host_helper_os import FileProjects
from server.host_helper_requests import answer_registered_read
from vibesys.api.transport import convert_run
from vs_project.api import Project
from vs_project.api.testing import FakeGitRepositories
from vs_proto.api import Oneof, ReadRequest, ReadResponse, validate
from vs_proto.api import host_payloads as pb
from vs_runlog.api import RunLogReader
from vs_sim.api import SeededRandom
from vs_sim.api.testing import ManualClock, SimNetwork, SimThreads

CORPUS = Path(__file__).parent / "fixtures/seam-history"
SHAPES = json.loads((CORPUS / "history.json").read_text())
AT = 1_800_000_000.0


def plant(
    project: Project, run_id: str, shape: str, random: SeededRandom, *, recent: bool = False
) -> bytes:
    """Draw from historical fields, never filling them with current model defaults."""
    entry = SHAPES[shape]
    events: list[dict[str, Any]] = [
        dict(event, run_id=run_id) for event in json.loads(json.dumps(entry["events"]))
    ]
    if shape == "missing-identity":
        label = events[1]["data"]["span"]["label"]
        label["attrs_json"] = json.dumps(
            {"role": random.choice(["implementer", "judge", "planner"])}
        )
    payload = b"".join(json.dumps(event).encode() + b"\n" for event in events)
    path = Project.event_journal_path_for(project.root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    wire = dict(entry["wire_event"], run_id=run_id)
    path.with_name(entry["wire_filename"]).write_text(json.dumps(wire) + "\n")
    age = random.randint(0, 299) if recent else random.randint(300, 3600)
    os.utime(path, (AT - age, AT - age))
    return payload


def rows(project: Project) -> pb.ListSavedRunsResponse:
    """The public serialized registry handler owns validation and discovery."""
    threads = SimThreads()
    host = replace(
        fake_host(threads, SimNetwork(threads)),
        projects=FileProjects(FakeGitRepositories().repository, threads, clock=ManualClock(AT)),
    )
    request = ReadRequest(
        checkout=str(project.root),
        method=Oneof(
            "list_saved_runs",
            pb.ListSavedRunsRequest(request_id="seam-list", project=str(project.root)),
        ),
    )
    response = ReadResponse.from_binary(
        answer_registered_read("list-saved-runs", request.to_binary(), host)
    )
    validate(response)
    assert response.result is not None
    assert response.result.field == "list_saved_runs"
    return response.result.value


def readable(project: Project, run_id: str) -> None:
    with Project.run_log_path_for(project.root, run_id).open("rb") as stream:
        records = list(RunLogReader(stream))
    assert records
    assert [record.seq for record in records] == list(range(1, len(records) + 1))


def test_shape_table_is_sanitized_and_pins_historical_bytes() -> None:
    for shape, entry in SHAPES.items():
        payload = (CORPUS / f"{shape}.jsonl").read_bytes()
        assert hashlib.sha256(payload).hexdigest() == entry["journal_sha256"]
        assert entry["events"] == [json.loads(line) for line in payload.splitlines()]
        for event in entry["events"]:
            assert set(event) <= set(entry["event_fields"])
            assert "execution_id" not in event


@given(seed=st.integers(min_value=1, max_value=2**32 - 1))
@example(seed=23)
def test_catalog_equals_generated_host_runs_with_stable_typed_refusals(seed: int) -> None:
    random = SeededRandom(seed)
    with TemporaryDirectory() as temporary, pytest.MonkeyPatch.context() as environment:
        root = Path(temporary)
        environment.setenv("VIBESYS_STATE_HOME", str(root / "state"))
        (root / "project").mkdir()
        project = Project.open(root / "project")
        shapes = list(SHAPES)
        random.shuffle(shapes)
        originals = {
            f"run-{index}": plant(project, f"run-{index}", shape, random)
            for index, shape in enumerate(shapes)
        }
        originals["recent"] = plant(project, "recent", "pre-lock", random, recent=True)
        originals["converted"] = plant(project, "converted", "pre-log", random)
        convert_run(project, "converted", clock=ManualClock(AT), threads=SimThreads())
        missing = f"run-{shapes.index('missing-identity')}"
        first, second = rows(project), rows(project)
        assert {row.run_id for row in first.runs} == set(originals)
        assert [(row.run_id, row.imported) for row in first.runs] == [
            (row.run_id, row.imported) for row in second.runs
        ]
        assert {
            row.run_id: row.detail
            for row in first.runs
            if row.imported == pb.SavedRunImport.UNAVAILABLE
        } == {
            row.run_id: row.detail
            for row in second.runs
            if row.imported == pb.SavedRunImport.UNAVAILABLE
        }
        for row in first.runs:
            if row.run_id == "recent":
                continue
            if row.run_id == missing:
                assert row.imported == pb.SavedRunImport.UNAVAILABLE
            elif row.run_id == f"run-{shapes.index('pre-trace')}":
                assert row.imported == pb.SavedRunImport.UNAVAILABLE
                assert row.detail == "journal predates the run trace"
            else:
                assert row.imported == pb.SavedRunImport.AVAILABLE
                readable(project, row.run_id)
        for run_id, original in originals.items():
            assert Project.event_journal_path_for(project.root, run_id).read_bytes() == original


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="#3430 unmerged: the refusal names the cause instead of dumping a validation error",
)
@given(seed=st.integers(min_value=1, max_value=2**32 - 1))
@example(seed=23)
def test_a_journal_that_cannot_convert_is_refused_with_a_plain_sentence(seed: int) -> None:
    random = SeededRandom(seed)
    with TemporaryDirectory() as temporary, pytest.MonkeyPatch.context() as environment:
        root = Path(temporary)
        environment.setenv("VIBESYS_STATE_HOME", str(root / "state"))
        (root / "project").mkdir()
        project = Project.open(root / "project")
        plant(project, "missing", "missing-identity", random)
        (row,) = rows(project).runs
        assert row.imported == pb.SavedRunImport.UNAVAILABLE
        assert row.detail.startswith("cannot convert:")


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="#3430 unmerged: a journal written within five minutes may have a live writer",
)
@given(seed=st.integers(min_value=1, max_value=2**32 - 1))
@example(seed=23)
def test_a_journal_a_live_writer_may_append_to_is_refused_not_converted(seed: int) -> None:
    random = SeededRandom(seed)
    with TemporaryDirectory() as temporary, pytest.MonkeyPatch.context() as environment:
        root = Path(temporary)
        environment.setenv("VIBESYS_STATE_HOME", str(root / "state"))
        (root / "project").mkdir()
        project = Project.open(root / "project")
        original = plant(project, "recent", "pre-lock", random, recent=True)
        (row,) = rows(project).runs
        assert row.imported == pb.SavedRunImport.UNAVAILABLE
        assert "live" in row.detail
        assert Project.event_journal_path_for(project.root, "recent").read_bytes() == original
