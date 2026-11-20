"""The `serving-studio` skill's API wrapper, driven through its command line (`main(argv)`).

A Fake transport plays the ServingStudio public API from a script of answers and a Fake clock
advances only when the wrapper sleeps, so no test opens a socket or waits in real time.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

if TYPE_CHECKING:
    from types import ModuleType

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "servingstudio.py"
API = "http://servingstudio.test/api/public/v1"
HEALTHY = {"status": "ok", "sim_commit": "abc123"}


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("servingstudio", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


ss = _load()


@dataclass
class FakeClock:
    time: float = 1_800_000_000.0
    slept: list[float] = field(default_factory=list)

    def now(self) -> float:
        return self.time

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.time += seconds


@dataclass
class FakeApi:
    """Answers /health, POST /simulate from a list, and each poll with the next status."""

    submits: list[tuple[int, dict[str, object], dict[str, str]]]
    statuses: list[str]
    health: tuple[int, dict[str, object]] = (200, HEALTHY)
    later_health: dict[str, object] | None = None
    unreachable: bool = False
    poll_failures: int = 0
    finished: int = 3
    calls: list[tuple[str, str]] = field(default_factory=list)

    def exchange(self, method: str, url: str, body: bytes | None) -> object:
        self.calls.append((method, url))
        if self.unreachable:
            raise ConnectionRefusedError("refused")
        if url.endswith("/health"):
            answer = self.health[1]
            if self.later_health is not None and any(m == "POST" for m, _ in self.calls):
                answer = self.later_health
            return ss.Response(self.health[0], {}, json.dumps(answer).encode())
        if method == "POST":
            status, answer, headers = self.submits.pop(0)
            return ss.Response(status, headers, json.dumps(answer).encode())
        if self.poll_failures:
            self.poll_failures -= 1
            raise ConnectionResetError("reset")
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        record: dict[str, object] = {"simulation_id": "s1", "status": status}
        if status == "done":
            record["summary"] = {"requests": {"total": 3, "finished": self.finished}}
        return ss.Response(200, {}, json.dumps(record).encode())


ACCEPTED = (202, {"simulation_id": "s1", "status": "queued", "routing": None}, {})


def _simulate(tmp_path: Path, api: FakeApi, clock: FakeClock | None = None) -> int:
    request = tmp_path / "req.json"
    request.write_text(json.dumps({"preset": "P/arch", "params": {"replicas": 1}}))
    return ss.main(
        ["simulate", "--request", str(request), "--out", str(tmp_path / "out")],
        transport=api,
        clock=clock or FakeClock(),
        environ={"SERVINGSTUDIO_API": API + "/"},
    )


def _provenance(tmp_path: Path) -> dict[str, object]:
    return json.loads((tmp_path / "out" / "provenance.json").read_text())


def test_health_prints_the_endpoint_and_its_revision(capsys: pytest.CaptureFixture[str]) -> None:
    api = FakeApi(submits=[], statuses=["done"])

    assert ss.main(["health"], transport=api, environ={"SERVINGSTUDIO_API": API}) == 0
    assert json.loads(capsys.readouterr().out) == {"endpoint": API, "sim_commit": "abc123"}


def test_health_defaults_to_the_hosted_service() -> None:
    api = FakeApi(submits=[], statuses=["done"])

    assert ss.main(["health"], transport=api, environ={}) == 0
    assert api.calls == [("GET", ss.DEFAULT_API + "/health")]


@pytest.mark.parametrize(
    ("api", "reason"),
    [
        (FakeApi(submits=[], statuses=[], health=(503, {"status": "down"})), "unhealthy"),
        (FakeApi(submits=[], statuses=[], unreachable=True), "unreachable"),
    ],
    ids=["unhealthy", "unreachable"],
)
def test_an_endpoint_that_does_not_answer_ok_exits_2(api: FakeApi, reason: str) -> None:
    assert ss.main(["health"], transport=api, environ={"SERVINGSTUDIO_API": API}) == 2, reason


def test_a_finished_run_is_kept_with_its_provenance(tmp_path: Path) -> None:
    api = FakeApi(submits=[ACCEPTED], statuses=["queued", "running", "done"])

    assert _simulate(tmp_path, api) == 0
    provenance = _provenance(tmp_path)
    assert provenance["endpoint"] == API
    assert provenance["sim_commit"] == "abc123"
    assert provenance["simulation_id"] == "s1"
    assert provenance["status"] == "done"
    assert provenance["preset"] == "P/arch"
    record = json.loads((tmp_path / "out" / "record.json").read_text())
    assert record["summary"] == {"requests": {"total": 3, "finished": 3}}
    assert provenance["complete"] is True
    assert provenance["revision_verified"] is True
    assert json.loads((tmp_path / "out" / "request.json").read_text())["preset"] == "P/arch"


def test_a_rate_limited_submission_waits_as_told_and_retries(tmp_path: Path) -> None:
    limited = (429, {"detail": "rate limited"}, {"Retry-After": "12"})
    api = FakeApi(submits=[limited, limited, ACCEPTED], statuses=["done"])
    clock = FakeClock()

    assert _simulate(tmp_path, api, clock) == 0
    assert clock.slept[:2] == [12.0, 12.0]


def test_a_submission_still_limited_after_every_attempt_exits_2(tmp_path: Path) -> None:
    limited = (429, {"detail": "rate limited"}, {})
    api = FakeApi(submits=[limited] * ss.SUBMIT_ATTEMPTS, statuses=["done"])

    assert _simulate(tmp_path, api) == 2


def test_a_refused_request_exits_2_and_keeps_the_refusal(tmp_path: Path) -> None:
    api = FakeApi(submits=[(400, {"detail": "no member"}, {})], statuses=["done"])

    assert _simulate(tmp_path, api) == 2
    assert not (tmp_path / "out" / "provenance.json").exists()
    refusal = json.loads((tmp_path / "out" / "submission-error.json").read_text())
    assert refusal["http_status"] == 400
    assert "no member" in refusal["body"]
    assert refusal["sim_commit"] == "abc123"


@given(
    middle=st.lists(st.sampled_from(["queued", "running"]), max_size=20),
    end=st.sampled_from(["done", "failed", "timed_out"]),
)
def test_a_run_exits_by_how_it_ended(
    tmp_path_factory: pytest.TempPathFactory, middle: list[str], end: str
) -> None:
    tmp_path = tmp_path_factory.mktemp("run")
    api = FakeApi(submits=[ACCEPTED], statuses=[*middle, end])

    assert _simulate(tmp_path, api) == (0 if end == "done" else 1)
    assert _provenance(tmp_path)["status"] == end


def test_a_run_that_never_ends_is_given_up_within_the_wait_limit(tmp_path: Path) -> None:
    api = FakeApi(submits=[ACCEPTED], statuses=["running"])
    clock = FakeClock()
    start = clock.time

    assert _simulate(tmp_path, api, clock) == 1
    assert _provenance(tmp_path)["status"] == "gave_up_waiting"
    assert clock.time - start <= ss.WAIT_LIMIT_SECONDS + ss.POLL_SECONDS


def test_an_unreadable_request_file_is_a_usage_error(tmp_path: Path) -> None:
    api = FakeApi(submits=[], statuses=[])
    code = ss.main(
        ["simulate", "--request", str(tmp_path / "missing.json"), "--out", str(tmp_path)],
        transport=api,
        environ={},
    )

    assert code == 64
    assert api.calls == []


def test_a_health_answer_without_a_revision_is_refused() -> None:
    api = FakeApi(submits=[], statuses=[], health=(200, {"status": "ok"}))

    assert ss.main(["health"], transport=api, environ={"SERVINGSTUDIO_API": API}) == 2


def test_a_long_retry_after_is_capped(tmp_path: Path) -> None:
    limited = (429, {"detail": "rate limited"}, {"Retry-After": "3600"})
    api = FakeApi(submits=[limited, ACCEPTED], statuses=["done"])
    clock = FakeClock()

    assert _simulate(tmp_path, api, clock) == 0
    assert clock.slept[0] == ss.MAX_RETRY_AFTER_SECONDS


@given(failures=st.integers(min_value=0, max_value=ss.POLL_FAILURES_TOLERATED))
def test_a_few_failed_polls_are_retried(
    tmp_path_factory: pytest.TempPathFactory, failures: int
) -> None:
    tmp_path = tmp_path_factory.mktemp("run")
    api = FakeApi(submits=[ACCEPTED], statuses=["done"], poll_failures=failures)

    assert _simulate(tmp_path, api) == 0
    assert _provenance(tmp_path)["status"] == "done"


def test_losing_contact_after_acceptance_keeps_the_simulation_id(tmp_path: Path) -> None:
    api = FakeApi(
        submits=[ACCEPTED], statuses=["done"], poll_failures=ss.POLL_FAILURES_TOLERATED + 1
    )

    assert _simulate(tmp_path, api) == 2
    provenance = _provenance(tmp_path)
    assert provenance["simulation_id"] == "s1"
    assert provenance["status"] == "lost_contact"


def test_a_revision_change_during_the_run_is_recorded(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    api = FakeApi(
        submits=[ACCEPTED], statuses=["done"], later_health={"status": "ok", "sim_commit": "def456"}
    )

    assert _simulate(tmp_path, api) == 3
    provenance = _provenance(tmp_path)
    assert (provenance["sim_commit"], provenance["sim_commit_after"]) == ("abc123", "def456")
    assert provenance["revision_verified"] is False
    assert "cannot be frozen" in capsys.readouterr().err


@given(finished=st.integers(min_value=0, max_value=2))
def test_a_done_run_with_unfinished_requests_is_not_a_result(
    tmp_path_factory: pytest.TempPathFactory, finished: int
) -> None:
    tmp_path = tmp_path_factory.mktemp("run")
    api = FakeApi(submits=[ACCEPTED], statuses=["done"], finished=finished)

    assert _simulate(tmp_path, api) == 1
    provenance = _provenance(tmp_path)
    assert provenance["status"] == "incomplete"
    assert provenance["complete"] is False


def test_a_directory_that_holds_a_result_is_never_overwritten(tmp_path: Path) -> None:
    assert _simulate(tmp_path, FakeApi(submits=[ACCEPTED], statuses=["done"])) == 0
    before = (tmp_path / "out" / "provenance.json").read_text()
    again = FakeApi(submits=[ACCEPTED], statuses=["done"])

    assert _simulate(tmp_path, again) == 64
    assert again.calls == []
    assert (tmp_path / "out" / "provenance.json").read_text() == before


REV = "e3ace30520b20a116d9df497f2f56bdfca300d51"
FITTED_ON: dict[str, object] = {
    "commit": "0123456789abcdef",
    "job": "j-17",
    "metric": "output_tok_s",
    "measured": 194.0,
    "timeline": "/scratch/profile-head/steps.jsonl",
}
CALIBRATION: dict[str, object] = {
    "simulator": {"checkout": "/sims/ServingStudioSim", "revision": REV, "pinned_by": "pins.toml"},
    "preset": "/records/presets/head.yaml",
    "workload": "trace: 64 requests, 2048 in / 512 out, 50 ms apart",
    "fitted_on": [FITTED_ON],
    "factors": {"arch.kind_time_scale.moe_prefill": 1.12},
    "residual_before": {"output_tok_s": 0.18},
    "residual_after": {"output_tok_s": -0.02},
    "engine_limitations_not_fitted": ["first-use stalls after warm-up"],
    "commands": ["uv run --no-sync python -m launcher head.yaml --no-gpu"],
    "result_dirs": ["/scratch/sim-head-fit"],
    "supersedes": None,
}
TODAY = {"name": "today", "overrides": {}}
FASTER_MOE = {"name": "moe_prefill_2x", "overrides": {"arch.kind_time_scale.moe_prefill": 0.5}}
ENGINE_STEP: dict[str, object] = {
    "name": "engine",
    "overrides": {},
    "values": {"64": 194.0},
    "command": "c0",
    "result_dir": "/scratch/l0",
}
CHUNK_STEP: dict[str, object] = {
    "name": "+ chunk 512",
    "overrides": {"worker.max_batch_tokens": 512},
    "values": {"64": 240.0},
    "command": "c1",
    "result_dir": "/scratch/l1",
}
BEST: dict[str, object] = {
    "concurrency": 64,
    "overrides": {"worker.max_batch_tokens": 512},
    "value": 240.0,
    "command": "c1",
    "result_dir": "/scratch/l1",
}
STUDY: dict[str, object] = {
    "simulator": {"checkout": "/sims/ServingStudioSim", "revision": REV},
    "preset": "/records/presets/head.yaml",
    "workload": "trace: 64 requests, 2048 in / 512 out, 50 ms apart",
    "metric": "output_tok_s",
    "calibration": "calibration-001",
    "axes": [
        {"name": "worker.max_batch_tokens", "values": [512, 2048, 4096], "assumption": None},
        {"name": "worker.dp_placement", "values": ["least-loaded"], "assumption": "engine only"},
    ],
    "scenarios": [TODAY, FASTER_MOE],
    "ladder": [ENGINE_STEP, CHUNK_STEP],
    "best": [BEST],
}


def _keep(tmp_path: Path, kind: str, record: object) -> int:
    file = tmp_path / "record.json"
    file.write_text(json.dumps(record))
    return ss.main(["record", "--kind", kind, "--file", str(file), "--dir", str(tmp_path / "r")])


def _kept(tmp_path: Path) -> list[str]:
    return sorted(p.name for p in (tmp_path / "r").glob("*.json"))


def test_calibrations_are_appended_each_superseding_the_latest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _keep(tmp_path, "calibration", CALIBRATION) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "calibration-001"

    assert _keep(tmp_path, "calibration", CALIBRATION) == 64  # must supersede calibration-001
    assert _keep(tmp_path, "calibration", {**CALIBRATION, "supersedes": "calibration-001"}) == 0
    assert _kept(tmp_path) == ["calibration-001.json", "calibration-002.json"]
    assert json.loads((tmp_path / "r" / "calibration-001.json").read_text()) == CALIBRATION


@pytest.mark.parametrize("source", ["measure", "timed-run"])
def test_a_calibration_says_what_it_was_fitted_on(tmp_path: Path, source: str) -> None:
    record = {**CALIBRATION, "fitted_on": [{**FITTED_ON, "source": source}]}

    assert _keep(tmp_path, "calibration", record) == 0
    assert json.loads((tmp_path / "r" / "calibration-001.json").read_text()) == record


@given(count=st.integers(min_value=1, max_value=12))
def test_records_are_numbered_in_order_and_never_replaced(count: int) -> None:
    with tempfile.TemporaryDirectory() as scratch:
        tmp_path = Path(scratch)
        for n in range(count):
            assert _keep(tmp_path, "study", {**STUDY, "metric": f"m{n}"}) == 0
        kept = _kept(tmp_path)
        assert kept == [f"study-{n:03d}.json" for n in range(1, count + 1)]
        for n, name in enumerate(kept):
            assert json.loads((tmp_path / "r" / name).read_text())["metric"] == f"m{n}"


@pytest.mark.parametrize("key", sorted(CALIBRATION))
def test_a_calibration_missing_a_key_is_refused_naming_it(
    tmp_path: Path, key: str, capsys: pytest.CaptureFixture[str]
) -> None:
    record = {k: v for k, v in CALIBRATION.items() if k != key}

    assert _keep(tmp_path, "calibration", record) == 64
    assert key in capsys.readouterr().err
    assert not (tmp_path / "r").exists()


@pytest.mark.parametrize(
    ("kind", "change", "message"),
    [
        ("calibration", {"extra": 1}, "unknown key extra"),
        ("calibration", {"factors": {}}, "factors"),
        ("calibration", {"factors": {"x": 0}}, "factors.x"),
        ("calibration", {"residual_after": {}}, "residual_after"),
        ("calibration", {"simulator": {"checkout": "/s", "revision": "main"}}, "revision"),
        ("calibration", {"fitted_on": [{**FITTED_ON, "timeline": ""}]}, "fitted_on[0].timeline"),
        ("calibration", {"fitted_on": [{**FITTED_ON, "source": "guess"}]}, "fitted_on[0].source"),
        ("study", {"axes": [{"name": "a", "values": [1, 2], "assumption": "x"}]}, "axes[0]"),
        ("study", {"axes": [{"name": "a", "values": [1], "assumption": None}]}, "no axis"),
        ("study", {"scenarios": [FASTER_MOE, FASTER_MOE]}, "today's kernels"),
        ("study", {"ladder": [CHUNK_STEP]}, "ladder[0]"),
        ("study", {"ladder": [ENGINE_STEP, CHUNK_STEP, ENGINE_STEP]}, "not cumulative"),
        ("study", {"best": [BEST, BEST]}, "listed twice"),
    ],
)
def test_a_record_that_breaks_its_format_is_refused_naming_the_key(
    tmp_path: Path,
    kind: str,
    change: dict[str, object],
    message: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    record = {**(CALIBRATION if kind == "calibration" else STUDY), **change}

    assert _keep(tmp_path, kind, record) == 64
    assert message in capsys.readouterr().err
    assert not (tmp_path / "r").exists()


def test_a_record_needs_a_directory(tmp_path: Path) -> None:
    file = tmp_path / "record.json"
    file.write_text(json.dumps(STUDY))
    records = {"SERVINGSTUDIO_RECORDS": str(tmp_path / "r")}

    assert ss.main(["record", "--kind", "study", "--file", str(file)], environ={}) == 64
    assert ss.main(["record", "--kind", "study", "--file", str(file)], environ=records) == 0
