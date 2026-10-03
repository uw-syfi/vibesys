from __future__ import annotations

from typing import TYPE_CHECKING

from hypothesis import given
from hypothesis import strategies as st
from resources.profilers.rocprof import remote_capture
from resources.profilers.rocprof.remote_capture import capture_runtime

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

_STATUS = capture_runtime.CaptureStatus
_WORKLOAD_RAN = {_STATUS.OK, _STATUS.KILLED_AFTER_GRACE}


def _capture(profiles: Path, capture_id: str, status: capture_runtime.CaptureStatus) -> None:
    directory = profiles / capture_id
    directory.mkdir(parents=True, exist_ok=True)
    capture_runtime.write_manifest(
        directory,
        {"capture_id": capture_id, "status": status.value, "load_returncode": 1},
    )


@given(statuses=st.lists(st.sampled_from(list(_STATUS)), min_size=1, max_size=4))
def test_a_trace_is_profile_evidence_only_when_every_workload_ran(
    tmp_path_factory: pytest.TempPathFactory, statuses: list[capture_runtime.CaptureStatus]
) -> None:
    """Regression (r18): a load_failed capture printed its load-window trace and exited 0."""
    profiles = tmp_path_factory.mktemp("profiles")
    ids = [f"timeline-{index}" for index in range(len(statuses))]
    for capture_id, status in zip(ids, statuses, strict=True):
        _capture(profiles, capture_id, status)

    failure = remote_capture.workload_failure(profiles, ids)

    if all(status in _WORKLOAD_RAN for status in statuses):
        assert failure is None
    else:
        first = next(s for s in statuses if s not in _WORKLOAD_RAN)
        assert failure is not None
        assert failure.startswith("not profilable: the configured workload did not run")
        assert f"status={first.value}" in failure


def test_a_capture_without_a_manifest_is_not_profile_evidence(tmp_path: Path) -> None:
    (tmp_path / "timeline-0").mkdir()

    failure = remote_capture.workload_failure(tmp_path, ["timeline-0"])

    assert failure is not None
    assert "no readable manifest" in failure
