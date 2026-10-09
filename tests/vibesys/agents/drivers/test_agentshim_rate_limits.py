"""A provider's rate-limit report reaches the run as a typed semantic event.

The turn is scripted in Claude's real stream format (a ``rate_limit_event``
frame ahead of the answer), so the whole path runs: agentshim parses the
frame, the driver translates it, ``AgentClient`` logs it for the operator and
``CoreAgentEventSink`` records it as ``CoreEventType.RATE_LIMIT_UPDATE``.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from agentshim.testing import FakeExecutor, FakeRun, scripted_turn
from hypothesis import given
from hypothesis import strategies as st

from vibesys.events import CoreEvent, CoreEventType, RateLimitUpdateData
from vibesys.run import CoreAgentEventSink, EventJournal
from vs_agent.api import AgentClient, AgentRateLimit
from vs_agent.drivers.agentshim import AgentShimDriver
from vs_sandbox.api import SANDBOX_DISABLE_ENV

STATUSES = ("allowed", "allowed_warning", "rejected", None)
WINDOW_NAMES = ("five_hour", "seven_day", "seven_day_opus")


@pytest.fixture(scope="module")
def home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A throwaway operator HOME: the driver prepares provider state under it."""
    return tmp_path_factory.mktemp("operator-home")


@st.composite
def _frames(draw: st.DrawFn) -> dict[str, object]:
    """A ``rate_limit_info`` object with a generated set of windows."""
    names = draw(st.lists(st.sampled_from(WINDOW_NAMES), min_size=1, max_size=3, unique=True))
    windows = {
        name: {
            "utilization": draw(st.floats(min_value=0.0, max_value=1.5)),
            "resetsAt": draw(st.integers(min_value=1_700_000_000, max_value=1_900_000_000)),
        }
        for name in names
    }
    info: dict[str, object] = {"unifiedWindows": windows}
    status = draw(st.sampled_from(STATUSES))
    if status is not None:
        info["status"] = status
        info["rateLimitType"] = draw(st.sampled_from(names))
    return info


def _expected_exhausted(info: dict[str, object], window: str, used: float) -> bool:
    """What a consumer must conclude: the provider's word, else usage at 100%."""
    if info.get("rateLimitType") == window and info.get("status") in {
        "allowed",
        "allowed_warning",
    }:
        return False
    if info.get("rateLimitType") == window and info.get("status") == "rejected":
        return True
    return used >= 1.0


def _run_turn(info: dict[str, object], root: Path, home: Path) -> tuple[list[CoreEvent], str]:
    """Run one Claude turn that reports *info*; return the run's events and its log."""
    turn = scripted_turn("claude", text="done")
    frame = json.dumps({"type": "rate_limit_event", "rate_limit_info": info}) + "\n"
    fake = FakeExecutor(FakeRun(stdout=[frame, *turn.stdout]))
    journal = EventJournal()
    journal.attach(root / "run", "run-1")
    log = io.StringIO()
    client = AgentClient(
        AgentShimDriver(
            provider="claude",
            executor_factory=lambda: fake,
            launcher_env=lambda: {
                "PATH": "/usr/bin:/bin",
                "HOME": str(home),
                SANDBOX_DISABLE_ENV: "off",
            },
        ),
        provider="claude",
        run_log_file=log,
        event_sink=CoreAgentEventSink(journal.record),
    )
    client.invoke_text(
        kind="implementer",
        workspace=root,
        system_prompt="sys",
        user_prompt="go",
        round_label="round-1",
        invocation_id="inv-1",
    )
    return list(journal.read()), log.getvalue()


@given(info=_frames())
def test_every_reported_window_becomes_one_typed_event_with_resolved_exhaustion(
    info: dict[str, object], home: Path
) -> None:
    with TemporaryDirectory() as tmp:
        events, log = _run_turn(info, Path(tmp), home)

    updates = [e for e in events if e.type is CoreEventType.RATE_LIMIT_UPDATE]
    windows = info["unifiedWindows"]
    assert isinstance(windows, dict)
    datas = [e.data for e in updates if isinstance(e.data, RateLimitUpdateData)]
    assert [d.window for d in datas] == list(windows)
    for update, data in zip(updates, datas, strict=True):
        assert data.window is not None
        reported = windows[data.window]
        assert data.provider == "claude"
        assert data.used_fraction == reported["utilization"]
        assert data.resets_at == float(reported["resetsAt"])
        assert data.exhausted is _expected_exhausted(info, data.window, reported["utilization"])
        assert update.agent_kind == "implementer"
        assert update.execution_id == "inv-1"
        # The operator's run log says so too, and names an exhausted window loudly.
        assert f"claude {data.window}: {'EXHAUSTED' if data.exhausted else 'ok'}" in log


def test_a_rejected_window_is_logged_as_exhausted_with_its_reset_time(
    tmp_path: Path, home: Path
) -> None:
    info: dict[str, object] = {
        "status": "rejected",
        "rateLimitType": "five_hour",
        "unifiedWindows": {"five_hour": {"utilization": 1.0, "resetsAt": 1_791_954_019}},
    }

    _events, log = _run_turn(info, tmp_path, home)

    assert "[rate limit] claude five_hour: EXHAUSTED, 100% used, resets 2026-10-14" in log


@given(
    exhausted=st.none() | st.booleans(),
    used=st.none() | st.floats(min_value=0.0, max_value=3.0, allow_nan=False),
)
def test_the_providers_own_statement_wins_over_the_usage_fraction(
    *, exhausted: bool | None, used: float | None
) -> None:
    limit = AgentRateLimit(window="w", used_fraction=used, exhausted=exhausted)

    if exhausted is not None:
        assert limit.is_exhausted is exhausted
    else:
        assert limit.is_exhausted is (used is not None and used >= 1.0)
