"""The SkyPilot lifecycle translator: forward only, total, and never guessing."""

from __future__ import annotations

import json

from hypothesis import given
from hypothesis import strategies as st

import vs_sandbox.skypilot_evaluator as evaluator_helper  # test-isolation: the standalone mounted helper re-states the result vocabulary; this pins it to the authoritative one.
from vs_sandbox.api.skypilot import (
    INITIAL_JOB_LIFE,
    ClusterEnd,
    ClusterEnded,
    ClusterInfo,
    ClusterInitializing,
    ClusterLife,
    ClusterUnknown,
    ClusterUp,
    Finished,
    Indeterminate,
    InvocationResultRecord,
    JobEnd,
    JobEnded,
    JobLife,
    JobQueued,
    JobRunning,
    JobStatus,
    JobUnknown,
    LogsFailed,
    ResultFrame,
    SkyClusterState,
    SkyJobState,
    StatusText,
    advance,
    allocation_gone,
    cluster_life,
    decode_response,
    encode_message,
    is_driver_failure,
    job_reading,
    logs_verdict,
    observe,
    status_text,
)

_JOB_STATES = [state.value for state in SkyJobState]
_CLUSTER_STATES = [state.value for state in SkyClusterState]
_NEAR_MISSES = ["running", "Running", " RUNNING", "RUNNING ", "SUCCEEDED+", "UP ", "up", "", "NONE"]
_TEXT = st.one_of(
    st.sampled_from(_JOB_STATES + _CLUSTER_STATES),
    st.sampled_from(_NEAR_MISSES),
    st.text(max_size=12),
    st.none(),
)
_RAW = st.builds(StatusText, _TEXT)
_RANK = {"unknown": -1, "queued": 0, "running": 1, "ended": 2}


@given(raws=st.lists(_RAW, max_size=30))
def test_observe_is_monotone_over_any_raw_sequence(raws: list[StatusText]) -> None:
    """The held life never moves to an earlier phase and never leaves Ended."""
    held: JobLife = INITIAL_JOB_LIFE
    for raw in raws:
        after = observe(held, raw)
        assert _RANK[after.kind] >= _RANK[held.kind]
        if isinstance(held, JobEnded):
            assert after == held
        held = after


@given(raws=st.lists(_RAW, min_size=1, max_size=30))
def test_an_unrecognised_reading_never_replaces_a_known_life(raws: list[StatusText]) -> None:
    held: JobLife = INITIAL_JOB_LIFE
    for raw in raws:
        after = observe(held, raw)
        if not isinstance(held, JobUnknown):
            assert not isinstance(after, JobUnknown)
        held = after


@given(raw=_RAW, prev=st.sampled_from([INITIAL_JOB_LIFE, JobQueued(), JobRunning()]))
def test_every_raw_reading_maps_to_a_defined_life(raw: StatusText, prev: JobLife) -> None:
    assert observe(prev, raw).kind in set(_RANK)
    assert cluster_life(raw).kind in {"initializing", "up", "ended", "unknown"}


@given(raw=_RAW)
def test_only_the_exact_documented_spelling_is_known(raw: StatusText) -> None:
    """No case folding, trimming, or default: anything else keeps its text as Unknown."""
    job = job_reading(raw)
    cluster = cluster_life(raw)
    if raw.text in _JOB_STATES:
        assert not isinstance(job, JobUnknown)
    else:
        assert job == JobUnknown(raw=raw.text)
    if raw.text in _CLUSTER_STATES:
        assert not isinstance(cluster, ClusterUnknown)
    else:
        assert cluster == ClusterUnknown(raw=raw.text)


def test_a_missing_status_is_unknown_not_running() -> None:
    assert job_reading(status_text(None)) == JobUnknown(raw=None)
    assert cluster_life(status_text(None)) == ClusterUnknown(raw=None)


@given(first=st.sampled_from(_JOB_STATES), second=st.sampled_from(_JOB_STATES))
def test_nothing_follows_an_end(first: str, second: str) -> None:
    ended = observe(INITIAL_JOB_LIFE, StatusText(first))
    if isinstance(ended, JobEnded):
        assert observe(ended, StatusText(second)) == ended


def test_a_stale_running_listing_after_success_is_ignored() -> None:
    done = observe(INITIAL_JOB_LIFE, StatusText("SUCCEEDED"))
    assert observe(done, StatusText("RUNNING")) == done == JobEnded(outcome=JobEnd.OK)
    assert advance(JobRunning(), JobQueued()) == JobRunning()


@given(state=st.sampled_from(_JOB_STATES))
def test_each_documented_end_has_its_own_outcome(state: str) -> None:
    life = job_reading(StatusText(state))
    if isinstance(life, JobEnded):
        assert (
            life.outcome.name
            == {
                "SUCCEEDED": "OK",
                "FAILED": "FAILED",
                "FAILED_SETUP": "SETUP_FAILED",
                "FAILED_DRIVER": "DRIVER_FAILED",
                "CANCELLED": "CANCELLED",
            }[state]
        )
    assert is_driver_failure(life) == (state == "FAILED_DRIVER")


@given(text=_TEXT)
def test_only_a_stopped_or_down_cluster_is_provably_gone(text: str | None) -> None:
    life: ClusterLife = cluster_life(StatusText(text))
    gone = allocation_gone(ClusterInfo("c", life))
    assert gone == (text in {"STOPPED", "DOWN"})
    assert isinstance(life, (ClusterEnded, ClusterInitializing, ClusterUp, ClusterUnknown))
    assert allocation_gone(None)
    if gone:
        assert isinstance(life, ClusterEnded)
        assert life.outcome in set(ClusterEnd)


@given(code=st.integers(-300, 300))
def test_logs_exit_codes_are_total_and_never_guess_a_finish(code: int) -> None:
    verdict = logs_verdict(code)
    if code in {0, 100, 103}:
        assert isinstance(verdict, Finished)
        assert verdict.status == {0: JobStatus.COMPLETED, 100: JobStatus.APPLICATION_FAILED}.get(
            code, JobStatus.CANCELLED
        )
    elif code in {101, 102}:
        assert verdict == Indeterminate(code)
    else:
        assert verdict == LogsFailed(code)


def test_the_standalone_evaluator_helper_accepts_exactly_the_protocol_statuses() -> None:
    """The mounted helper cannot import the protocol, so its copy is pinned here."""
    assert {status.value for status in JobStatus} == evaluator_helper.RESULT_STATUSES
    assert evaluator_helper.exit_status_of(JobStatus.COMPLETED.value) == 0
    assert evaluator_helper.exit_status_of(JobStatus.CANCELLED.value) == 130
    assert evaluator_helper.exit_status_of(JobStatus.APPLICATION_FAILED.value) == 1


def test_recorded_wire_json_with_plain_status_strings_still_decodes() -> None:
    """The result status is a JobStatus in Python and the same plain string on the wire."""
    frame = decode_response(
        b'{"version":2,"type":"result","status":"APPLICATION_FAILED","sky_exit_code":100,'
        b'"remote_job_id":7}'
    )
    assert isinstance(frame, ResultFrame)
    assert frame.status is JobStatus.APPLICATION_FAILED
    assert json.loads(encode_message(frame))["status"] == "APPLICATION_FAILED"

    recorded = {
        "status": "COMPLETED",
        "sky_exit_code": 0,
        "artifact": None,
        "provenance": {
            "profile_name": "p",
            "infra": "i",
            "cluster_name": "c",
            "job_name": "j",
            "remote_job_id": 9,
            "attempt": 1,
            "accelerator_type": "A100",
            "nodes": 1,
            "accelerators_per_node": 1,
        },
    }
    record = InvocationResultRecord.model_validate_json(json.dumps(recorded))
    assert record.status is JobStatus.COMPLETED
    assert json.loads(record.model_dump_json())["status"] == "COMPLETED"
