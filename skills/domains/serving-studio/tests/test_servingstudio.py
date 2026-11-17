"""The `serving-studio` skill's API wrapper, driven through its command line (`main(argv)`).

A Fake transport plays the ServingStudio public API from a script of answers and a Fake clock
advances only when the wrapper sleeps, so no test opens a socket or waits in real time.
"""

from __future__ import annotations

import importlib.util
import json
import sys
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
