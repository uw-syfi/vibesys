"""A schema or input change that left a native golden stale is named from hashes alone."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.native_goldens import report_stale, stale_goldens
from tests.support.wire_goldens import REGENERATE

from vs_proto.api import ClientReplay, ClientReplayBatch, EpochStarted, Lifecycle, Oneof, Record

if TYPE_CHECKING:
    from pathlib import Path

_API, _STORE, _FINGERPRINT = "a" * 64, "b" * 64, "c" * 64


def _golden(directory: Path, name: str, *, api: str = _API, store: str = _STORE) -> None:
    opening = Record(
        seq=1,
        epoch=1,
        body=Oneof("epoch_started", EpochStarted(api_schema_hash=api, store_schema_hash=store)),
    )
    replay = ClientReplay(
        batches=[ClientReplayBatch(source_sequence=1, records=[opening.to_binary()])]
    )
    (directory / name).write_bytes(replay.to_binary())


def _publish(directory: Path, *, fingerprint: str = _FINGERPRINT) -> None:
    """The manifests the golden generators write beside the goldens."""
    (directory / "source.sha256").write_text(fingerprint + "\n")
    (directory / "goldens.sha256").write_text(
        "".join(
            f"{path.name} {hashlib.sha256(path.read_bytes()).hexdigest()}\n"
            for path in sorted(directory.glob("*.pb"))
        )
    )


def _stale(
    directory: Path, *, fingerprint: str = _FINGERPRINT, api: str = _API, store: str = _STORE
) -> list[str]:
    return stale_goldens(
        directory,
        label="goldens",
        fingerprint=fingerprint,
        api_schema_hash=api,
        store_schema_hash=store,
    )


def test_goldens_made_from_the_current_inputs_are_not_stale(tmp_path: Path) -> None:
    _golden(tmp_path, "one.pb")
    _golden(tmp_path, "two.pb")
    (tmp_path / "delta.pb").write_bytes(
        ClientReplay(
            batches=[
                ClientReplayBatch(
                    source_sequence=2,
                    records=[
                        Record(seq=2, epoch=1, body=Oneof("lifecycle", Lifecycle())).to_binary()
                    ],
                )
            ]
        ).to_binary()
    )
    _publish(tmp_path)

    assert _stale(tmp_path) == []


@pytest.mark.parametrize(
    ("api", "store", "stale"),
    [
        (_API, _STORE, 0),
        ("d" * 64, _STORE, 1),
        (_API, "d" * 64, 1),
        ("d" * 64, "e" * 64, 2),
    ],
    ids=["current", "api-hash", "store-hash", "both"],
)
def test_a_golden_embedding_an_old_schema_hash_is_named(
    tmp_path: Path, api: str, store: str, stale: int
) -> None:
    _golden(tmp_path, "scenario.pb", api=api, store=store)
    _publish(tmp_path)

    problems = _stale(tmp_path)

    assert len(problems) == stale
    assert all(problem.startswith("goldens/scenario.pb embeds ") for problem in problems)


def test_changed_golden_inputs_are_named(tmp_path: Path) -> None:
    _golden(tmp_path, "scenario.pb")
    _publish(tmp_path, fingerprint="d" * 64)

    assert _stale(tmp_path) == [
        "goldens/source.sha256 records golden inputs dddddddddddd, which now hash to cccccccccccc"
    ]


def test_golden_bytes_that_the_manifest_does_not_list_are_named(tmp_path: Path) -> None:
    _golden(tmp_path, "scenario.pb")
    _publish(tmp_path)
    (tmp_path / "scenario.pb").write_bytes((tmp_path / "scenario.pb").read_bytes() + b"\x00")
    _golden(tmp_path, "new.pb")

    assert _stale(tmp_path) == [
        "goldens/new.pb is not the bytes goldens.sha256 lists",
        "goldens/scenario.pb is not the bytes goldens.sha256 lists",
        "goldens/scenario.pb does not decode as a replay",
    ]


def test_a_listed_golden_that_is_gone_is_named(tmp_path: Path) -> None:
    _golden(tmp_path, "scenario.pb")
    _golden(tmp_path, "gone.pb")
    _publish(tmp_path)
    (tmp_path / "gone.pb").unlink()

    assert _stale(tmp_path) == ["goldens/goldens.sha256 lists gone.pb, which is gone"]


@given(count=st.integers(min_value=1, max_value=60))
def test_the_report_is_short_and_names_the_regeneration_command(count: int) -> None:
    report = report_stale(
        [f"goldens/g{index}.pb embeds API schema hash aaaaaaaaaaaa" for index in range(count)]
    )

    assert report.endswith(f"Regenerate and commit them: {REGENERATE}")
    assert len(report.splitlines()) <= 13
