#!/usr/bin/env python3
"""Unit tests for the multiturn benchmark: metrics writer, hard-fail rules, pacing model.

Covers ``write_metrics``, ``write_turn_records``, ``validate_attempt`` and
``check_all_turns_ok`` (the drop/truncation/error hard-fail), ``compute_schedule``
(pure, deterministic), the send-time rule in ``run_session``
(``max(scheduled_time, completion_time + think_time)``) using a fake clock and
a fake HTTP server, and ``aggregate_metrics``'s reduction. No real server, no
GPU. Run with:

    uv run pytest examples/model-serving/qwen3.5-397b-a17b-mi300a-bespoke/benchmark/test_run.py -q --no-cov -p no:tach

Skips cleanly if ``run.py`` cannot be imported (e.g. ``aiohttp`` missing).
"""

from __future__ import annotations

import asyncio
import heapq
import json
import math
import statistics
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

try:
    import run
except Exception as exc:  # noqa: BLE001 -- env-dependent import; skip, don't fail.
    run = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


REF_TTFT_MS = 700.0
REF_TPOT_MS = 110.0


@unittest.skipIf(run is None, f"could not import run.py: {_IMPORT_ERROR}")
class WriteMetricsTests(unittest.TestCase):
    def test_primary_metric_is_top_level_in_flat_json(self) -> None:
        values = {"p95_ttft_turn2plus_ms": 123.4, "mean_tpot_ms": 12.3}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            run.write_metrics(path, values)
            self.assertEqual(json.loads(path.read_text()), values)

    def test_metrics_names_include_primary(self) -> None:
        self.assertIn("p95_ttft_turn2plus_ms", run.METRICS)


def _attempt(text: str = "hi", completion_tokens: int | None = 10) -> run.TurnAttempt:
    return run.TurnAttempt(text, 0.1, completion_tokens, 5, None, 1.0, None)


@unittest.skipIf(run is None, f"could not import run.py: {_IMPORT_ERROR}")
class ValidateAttemptTests(unittest.TestCase):
    def test_exact_budget_passes(self) -> None:
        self.assertIsNone(run.validate_attempt(_attempt(completion_tokens=10), 10))

    def test_truncated_fails(self) -> None:
        self.assertIn("truncated", run.validate_attempt(_attempt(completion_tokens=9), 10))

    def test_over_budget_fails(self) -> None:
        self.assertIn("truncated", run.validate_attempt(_attempt(completion_tokens=11), 10))

    def test_missing_usage_fails(self) -> None:
        self.assertIn("usage", run.validate_attempt(_attempt(completion_tokens=None), 10))

    def test_empty_text_fails(self) -> None:
        self.assertIn("empty", run.validate_attempt(_attempt(text=""), 10))


@unittest.skipIf(run is None, f"could not import run.py: {_IMPORT_ERROR}")
class CheckAllTurnsOkTests(unittest.TestCase):
    def _all_ok(self) -> list[run.TurnResult]:
        return [
            run.TurnResult(session_id=s.session_id, turn_index=i, ok=True)
            for s in run.generate_sessions(run.SEED, run.N_SESSIONS)
            for i in range(1, len(s.turns) + 1)
        ]

    def test_clean_run_passes(self) -> None:
        run.check_all_turns_ok(self._all_ok(), 1)

    def test_failed_turn_raises(self) -> None:
        results = self._all_ok()
        results[3].ok = False
        results[3].error = "truncated: 1 completion tokens, budget 90"
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            run.check_all_turns_ok(results, 1)

    def test_unattempted_turn_raises(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unattempted"):
            run.check_all_turns_ok(self._all_ok()[:-1], 1)

    def test_repetitions_scale_the_planned_count(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unattempted"):
            run.check_all_turns_ok(self._all_ok(), 2)


@unittest.skipIf(run is None, f"could not import run.py: {_IMPORT_ERROR}")
class TurnsOutputPathTests(unittest.TestCase):
    def test_appends_turns_jsonl_suffix(self) -> None:
        self.assertEqual(
            run.turns_output_path(Path("/tmp/out.json")),
            Path("/tmp/out.json.turns.jsonl"),
        )


@unittest.skipIf(run is None, f"could not import run.py: {_IMPORT_ERROR}")
class WriteTurnRecordsTests(unittest.TestCase):
    def _fake_results(self) -> list[run.TurnResult]:
        return [
            run.TurnResult(
                session_id=0,
                turn_index=1,
                ok=True,
                ttft_s=0.123,
                completion_tokens=42,
                latency_s=1.5,
                error=None,
                send_ts_monotonic=100.0,
                send_ts_wall=1_700_000_000.0,
                first_token_ts_wall=1_700_000_000.123,
                completion_ts_wall=1_700_000_001.5,
                prompt_tokens=17,
                cached_tokens=5,
                schedule_bound=True,
                scheduled_send_ts=99.5,
                scheduled_admission_delay_s=12.5,
            ),
            run.TurnResult(
                session_id=0,
                turn_index=2,
                ok=False,
                ttft_s=None,
                completion_tokens=None,
                latency_s=0.05,
                error="HTTP 500: boom",
                send_ts_monotonic=200.0,
                send_ts_wall=1_700_000_010.0,
                first_token_ts_wall=None,
                completion_ts_wall=1_700_000_010.05,
                prompt_tokens=None,
                cached_tokens=None,
                schedule_bound=None,
                scheduled_send_ts=None,
                scheduled_admission_delay_s=None,
            ),
        ]

    def test_writes_one_record_per_turn_with_expected_fields(self) -> None:
        results = self._fake_results()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.jsonl.turns.jsonl"
            run.write_turn_records(path, results)
            lines = path.read_text().splitlines()

        self.assertEqual(len(lines), len(results))
        first = json.loads(lines[0])
        self.assertEqual(first["session_id"], 0)
        self.assertEqual(first["turn_index"], 1)
        self.assertIs(first["ok"], True)
        self.assertEqual(first["send_ts_monotonic"], 100.0)
        self.assertEqual(first["send_ts_wall"], 1_700_000_000.0)
        self.assertAlmostEqual(first["ttft_ms"], 123.0)
        self.assertEqual(first["first_token_ts_wall"], 1_700_000_000.123)
        self.assertEqual(first["completion_ts_wall"], 1_700_000_001.5)
        self.assertEqual(first["output_tokens"], 42)
        self.assertEqual(first["prompt_tokens"], 17)
        self.assertEqual(first["cached_tokens"], 5)
        self.assertIs(first["schedule_bound"], True)
        self.assertEqual(first["scheduled_send_ts"], 99.5)
        self.assertEqual(first["scheduled_admission_delay_s"], 12.5)
        self.assertIsNone(first["error"])

        second = json.loads(lines[1])
        self.assertIs(second["ok"], False)
        self.assertIsNone(second["ttft_ms"])
        self.assertIsNone(second["output_tokens"])
        self.assertIsNone(second["schedule_bound"])
        self.assertIsNone(second["scheduled_send_ts"])
        self.assertIsNone(second["scheduled_admission_delay_s"])
        self.assertEqual(second["error"], "HTTP 500: boom")

    def test_empty_results_writes_empty_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.jsonl.turns.jsonl"
            run.write_turn_records(path, [])
            self.assertEqual(path.read_text(), "")


@unittest.skipIf(run is None, f"could not import run.py: {_IMPORT_ERROR}")
class ComputeScheduleTest(unittest.TestCase):
    """Tests for compute_schedule() against the real, generated workload."""

    def test_deterministic_given_seed(self) -> None:
        sessions = run.generate_sessions(run.SEED, n_sessions=8)
        start_delays = run.session_start_delays(len(sessions))
        first = run.compute_schedule(sessions, start_delays, REF_TTFT_MS, REF_TPOT_MS)
        second = run.compute_schedule(sessions, start_delays, REF_TTFT_MS, REF_TPOT_MS)
        self.assertEqual(first, second)

    def test_monotone_per_session(self) -> None:
        sessions = run.generate_sessions(run.SEED, n_sessions=8)
        start_delays = run.session_start_delays(len(sessions))
        schedule = run.compute_schedule(sessions, start_delays, REF_TTFT_MS, REF_TPOT_MS)
        for session in sessions:
            times = schedule[session.session_id]
            self.assertEqual(len(times), len(session.turns))
            for earlier, later in zip(times, times[1:], strict=False):
                self.assertLess(earlier, later)

    def test_turn_zero_is_start_delay(self) -> None:
        sessions = run.generate_sessions(run.SEED, n_sessions=8)
        start_delays = run.session_start_delays(len(sessions))
        schedule = run.compute_schedule(sessions, start_delays, REF_TTFT_MS, REF_TPOT_MS)
        for index, session in enumerate(sessions):
            self.assertEqual(schedule[session.session_id][0], start_delays[index])

    def test_matches_formula_on_a_synthetic_session(self) -> None:
        session = run.Session(
            session_id=0,
            turns=(
                run.TurnSpec(user_text="a", max_tokens=100, think_time_before_s=0.0),
                run.TurnSpec(user_text="b", max_tokens=50, think_time_before_s=3.0),
                run.TurnSpec(user_text="c", max_tokens=20, think_time_before_s=2.0),
            ),
        )
        schedule = run.compute_schedule([session], [0.0], REF_TTFT_MS, REF_TPOT_MS)
        t0 = 0.0
        t1 = t0 + 0.7 + 0.11 * 100 + 3.0
        t2 = t1 + 0.7 + 0.11 * 50 + 2.0
        got = schedule[0]
        self.assertAlmostEqual(got[0], t0, places=9)
        self.assertAlmostEqual(got[1], t1, places=9)
        self.assertAlmostEqual(got[2], t2, places=9)

    def test_ref_speed_scales_the_schedule(self) -> None:
        session = run.Session(
            session_id=0,
            turns=(
                run.TurnSpec(user_text="a", max_tokens=100, think_time_before_s=0.0),
                run.TurnSpec(user_text="b", max_tokens=50, think_time_before_s=3.0),
            ),
        )
        slow_ref = run.compute_schedule([session], [0.0], ref_ttft_ms=1400.0, ref_tpot_ms=220.0)
        fast_ref = run.compute_schedule([session], [0.0], ref_ttft_ms=350.0, ref_tpot_ms=55.0)
        # A slower assumed reference produces a later turn-2 schedule time.
        self.assertGreater(slow_ref[0][1], fast_ref[0][1])


@unittest.skipIf(run is None, f"could not import run.py: {_IMPORT_ERROR}")
class AdmissionQueueSimulationTest(unittest.TestCase):
    """T[s][0]: the discrete-event simulation of the CONCURRENCY-slot admission
    queue at the reference speed (see compute_schedule's docstring). Uses the
    real 48-session/CONCURRENCY=16 workload, since with fewer sessions than
    CONCURRENCY the queue never engages and these properties would be
    vacuous -- this is the exact regime job 632584 measured (sessions 16-47
    admitted 46-176s late in reality, ignored entirely by the pre-fix
    schedule).
    """

    def test_deterministic_under_contention(self) -> None:
        sessions = run.generate_sessions(run.SEED, run.N_SESSIONS)
        start_delays = run.session_start_delays(len(sessions))
        first = run.compute_schedule(sessions, start_delays, REF_TTFT_MS, REF_TPOT_MS)
        second = run.compute_schedule(sessions, start_delays, REF_TTFT_MS, REF_TPOT_MS)
        self.assertEqual(first, second)

    def test_admission_start_nondecreasing_in_arrival_order(self) -> None:
        # The c-server "earliest free slot first" simulation hands slots to
        # sessions processed in nondecreasing arrival order, so both the
        # start time and the slot's next-free time are nondecreasing in that
        # same order.
        sessions = run.generate_sessions(run.SEED, run.N_SESSIONS)
        start_delays = run.session_start_delays(len(sessions))
        schedule = run.compute_schedule(sessions, start_delays, REF_TTFT_MS, REF_TPOT_MS)
        starts = [schedule[session.session_id][0] for session in sessions]
        for earlier, later in zip(starts, starts[1:], strict=False):
            self.assertLessEqual(earlier, later)

    def test_first_concurrency_sessions_never_queue(self) -> None:
        # The first CONCURRENCY arrivals always find a free slot immediately:
        # T[s][0] == start_delays[s], exactly like the pre-fix schedule.
        sessions = run.generate_sessions(run.SEED, run.N_SESSIONS)
        start_delays = run.session_start_delays(len(sessions))
        schedule = run.compute_schedule(sessions, start_delays, REF_TTFT_MS, REF_TPOT_MS)
        for index in range(run.CONCURRENCY):
            self.assertAlmostEqual(
                schedule[sessions[index].session_id][0], start_delays[index], places=9
            )

    def test_sessions_past_concurrency_queue_for_admission(self) -> None:
        # This is the bug: sessions 16-47 (job 632584) must wait for a slot,
        # i.e. T[s][0] is strictly later than the raw start_delays stagger.
        sessions = run.generate_sessions(run.SEED, run.N_SESSIONS)
        start_delays = run.session_start_delays(len(sessions))
        schedule = run.compute_schedule(sessions, start_delays, REF_TTFT_MS, REF_TPOT_MS)
        for index in range(run.CONCURRENCY, len(sessions)):
            self.assertGreater(schedule[sessions[index].session_id][0], start_delays[index])

    def test_queueing_delay_matches_observed_regression_scale(self) -> None:
        # job 632584 measured real semaphore admission delays of 46-176s for
        # sessions 16-47. The reference-speed simulation should land in the
        # same ballpark: neither zero (the bug) nor absurdly larger.
        sessions = run.generate_sessions(run.SEED, run.N_SESSIONS)
        start_delays = run.session_start_delays(len(sessions))
        schedule = run.compute_schedule(sessions, start_delays, REF_TTFT_MS, REF_TPOT_MS)
        delays = [
            schedule[sessions[index].session_id][0] - start_delays[index]
            for index in range(run.CONCURRENCY, len(sessions))
        ]
        self.assertGreater(statistics.median(delays), 30.0)
        self.assertLess(max(delays), 400.0)


@unittest.skipIf(run is None, f"could not import run.py: {_IMPORT_ERROR}")
class ResolveConcurrencyTest(unittest.TestCase):
    """--concurrency: 0 means unlimited (benchmark_version 4's new default)."""

    def test_zero_resolves_to_n_sessions(self) -> None:
        self.assertEqual(run.resolve_concurrency(0, run.N_SESSIONS), run.N_SESSIONS)

    def test_positive_value_used_as_is(self) -> None:
        self.assertEqual(run.resolve_concurrency(16, run.N_SESSIONS), 16)

    def test_unlimited_semaphore_size_equals_n_sessions(self) -> None:
        # The real run-time cap is asyncio.Semaphore(resolve_concurrency(...)):
        # with --concurrency 0 that must be sized to admit every session at
        # once, i.e. exactly N_SESSIONS slots, never fewer.
        concurrency = run.resolve_concurrency(0, run.N_SESSIONS)
        semaphore = asyncio.Semaphore(concurrency)
        self.assertEqual(semaphore._value, run.N_SESSIONS)


@unittest.skipIf(run is None, f"could not import run.py: {_IMPORT_ERROR}")
class UnlimitedConcurrencyScheduleTest(unittest.TestCase):
    """compute_schedule under --concurrency 0 (unlimited): no admission wait at all."""

    def test_every_session_admitted_with_zero_queueing_delay(self) -> None:
        # With concurrency == n_sessions, _simulate_admission_starts hands
        # every arrival its own free slot immediately: T[s][0] == the raw
        # start_delays stagger for every session, not just the first
        # CONCURRENCY of them (contrast with
        # AdmissionQueueSimulationTest.test_sessions_past_concurrency_queue_for_admission,
        # where sessions past the fixed 16-slot cap do queue). This is the
        # "pure per-session pacing" case compute_schedule's docstring
        # describes.
        sessions = run.generate_sessions(run.SEED, run.N_SESSIONS)
        start_delays = run.session_start_delays(len(sessions))
        concurrency = run.resolve_concurrency(0, len(sessions))
        schedule = run.compute_schedule(
            sessions, start_delays, REF_TTFT_MS, REF_TPOT_MS, concurrency=concurrency
        )
        for index, session in enumerate(sessions):
            self.assertAlmostEqual(schedule[session.session_id][0], start_delays[index], places=9)

    def test_matches_unbounded_reference_offered_rate(self) -> None:
        # Sanity check on the same claim from a different angle: with no
        # admission queueing, every session's turn-1 send time is exactly its
        # stagger, so the last session to start is session N_SESSIONS - 1 at
        # (N_SESSIONS - 1) * SESSION_START_STAGGER_S, not later.
        sessions = run.generate_sessions(run.SEED, run.N_SESSIONS)
        start_delays = run.session_start_delays(len(sessions))
        concurrency = run.resolve_concurrency(0, len(sessions))
        schedule = run.compute_schedule(
            sessions, start_delays, REF_TTFT_MS, REF_TPOT_MS, concurrency=concurrency
        )
        last_admission = max(schedule[session.session_id][0] for session in sessions)
        self.assertAlmostEqual(
            last_admission,
            (run.N_SESSIONS - 1) * run.SESSION_START_STAGGER_S,
            places=9,
        )


@unittest.skipIf(run is None, f"could not import run.py: {_IMPORT_ERROR}")
class ConcurrencySixteenReproducesPreviousScheduleTest(unittest.TestCase):
    """--concurrency 16 must reproduce exactly the benchmark_version 3 schedule."""

    def test_explicit_sixteen_matches_default_concurrency_schedule(self) -> None:
        # compute_schedule's default parameter (concurrency=CONCURRENCY, i.e.
        # 16) is what every benchmark_version 3 baseline was measured
        # against; passing concurrency=16 explicitly (what --concurrency 16
        # now does) must produce the identical schedule.
        sessions = run.generate_sessions(run.SEED, run.N_SESSIONS)
        start_delays = run.session_start_delays(len(sessions))
        default_schedule = run.compute_schedule(sessions, start_delays, REF_TTFT_MS, REF_TPOT_MS)
        explicit_schedule = run.compute_schedule(
            sessions, start_delays, REF_TTFT_MS, REF_TPOT_MS, concurrency=16
        )
        self.assertEqual(default_schedule, explicit_schedule)

    def test_resolve_concurrency_sixteen_is_a_no_op(self) -> None:
        self.assertEqual(run.resolve_concurrency(16, run.N_SESSIONS), run.CONCURRENCY)


class _FakeResponse:
    """Minimal aiohttp-response stand-in for one streamed chat completion."""

    def __init__(self, ttft_s: float, total_s: float, n_tokens: int) -> None:
        self.status = 200
        self._ttft_s = ttft_s
        self._total_s = total_s
        self._n_tokens = n_tokens

    async def __aenter__(self) -> _FakeResponse:
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def text(self) -> str:
        return ""

    @property
    def content(self):
        return self._iter_lines()

    async def _iter_lines(self):
        await asyncio.sleep(self._ttft_s)
        yield b'data: {"choices":[{"delta":{"content":"x"}}]}\n'
        remaining = self._n_tokens - 1
        if remaining > 0:
            per_token_s = (self._total_s - self._ttft_s) / remaining
            for _ in range(remaining):
                await asyncio.sleep(per_token_s)
                yield b'data: {"choices":[{"delta":{"content":"x"}}]}\n'
        usage = json.dumps({"usage": {"completion_tokens": self._n_tokens}})
        yield f"data: {usage}\n".encode()
        yield b"data: [DONE]\n"


class _FakeClient:
    """Records the (fake-clock) send time of each request it is given."""

    def __init__(self, plan: list[tuple[float, float, int]]) -> None:
        self._plan = list(plan)
        self.send_times: list[float] = []

    def post(self, url: str, json: object, timeout: object) -> _FakeResponse:  # noqa: A002
        self.send_times.append(time.perf_counter())
        ttft_s, total_s, n_tokens = self._plan.pop(0)
        return _FakeResponse(ttft_s, total_s, n_tokens)


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def perf_counter(self) -> float:
        return self.now

    def advance(self, dt: float) -> None:
        self.now += dt


def _two_turn_session() -> run.Session:
    return run.Session(
        session_id=0,
        turns=(
            run.TurnSpec(user_text="a", max_tokens=100, think_time_before_s=0.0),
            run.TurnSpec(user_text="b", max_tokens=50, think_time_before_s=3.0),
        ),
    )


@unittest.skipIf(run is None, f"could not import run.py: {_IMPORT_ERROR}")
class SendTimeRuleTest(unittest.IsolatedAsyncioTestCase):
    """max(scheduled_time, completion_time_of_prev_turn + think_time), with a fake clock."""

    def setUp(self) -> None:
        self._clock = _FakeClock()
        real_sleep = asyncio.sleep

        async def fake_sleep(delay: float, result: object = None) -> object:
            self._clock.advance(delay)
            return await real_sleep(0, result)

        self._patches = [
            mock.patch("time.perf_counter", new=self._clock.perf_counter),
            mock.patch("asyncio.sleep", new=fake_sleep),
        ]
        for patch in self._patches:
            patch.start()
            self.addCleanup(patch.stop)

    async def test_fast_server_waits_for_the_schedule(self) -> None:
        session = _two_turn_session()
        schedule = run.compute_schedule([session], [0.0], REF_TTFT_MS, REF_TPOT_MS)[0]
        # T[1] = 0.7 + 0.11*100 + 3.0 = 14.7s. A fast turn-1 response (0.5s
        # total) plus 3s think time is ready at 3.5s, well before the
        # schedule: turn 2 must wait for it.
        client = _FakeClient(plan=[(0.05, 0.5, 100), (0.05, 0.5, 50)])
        results = await run.run_session(
            client,
            "http://fake",
            session,
            asyncio.Semaphore(1),
            start_delay_s=0.0,
            schedule=schedule,
            run_started=0.0,
        )
        self.assertEqual(len(results), 2)
        self.assertIsNone(results[0].schedule_bound)
        self.assertTrue(results[1].schedule_bound)
        self.assertAlmostEqual(client.send_times[1], schedule[1], places=6)
        self.assertAlmostEqual(results[1].scheduled_send_ts, schedule[1], places=6)
        # Single session, no admission contention: T[s][0] == start_delay_s,
        # so the simulated admission wait is zero.
        self.assertAlmostEqual(results[0].scheduled_admission_delay_s, 0.0, places=9)

    async def test_truncated_turn_fails_and_ends_the_session(self) -> None:
        session = _two_turn_session()  # turn 1 budget is 100 tokens
        client = _FakeClient(plan=[(0.05, 0.5, 99)])
        results = await run.run_session(
            client, "http://fake", session, asyncio.Semaphore(1), start_delay_s=0.0
        )
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].ok)
        self.assertIn("truncated", results[0].error)

    async def test_slow_server_degrades_to_closed_loop(self) -> None:
        session = _two_turn_session()
        schedule = run.compute_schedule([session], [0.0], REF_TTFT_MS, REF_TPOT_MS)[0]
        # A slow turn-1 response (20s total) plus 3s think time is ready at
        # 23s, past the 14.7s schedule: turn 2 must fire immediately, exactly
        # like closed-loop pacing, not wait any further.
        client = _FakeClient(plan=[(1.0, 20.0, 100), (0.05, 0.5, 50)])
        results = await run.run_session(
            client,
            "http://fake",
            session,
            asyncio.Semaphore(1),
            start_delay_s=0.0,
            schedule=schedule,
            run_started=0.0,
        )
        self.assertEqual(len(results), 2)
        self.assertFalse(results[1].schedule_bound)
        expected_ready_time = results[0].latency_s + session.turns[1].think_time_before_s
        self.assertAlmostEqual(client.send_times[1], expected_ready_time, places=6)

    async def test_closed_pacing_ignores_any_schedule(self) -> None:
        """--pacing closed: schedule=None, so send logic is exactly closed-loop.

        The admission-queue simulation lives entirely inside compute_schedule
        (called only under --pacing scheduled, see run_repetition); closed
        pacing never builds a schedule, so this test -- unmodified by the fix
        -- and test_closed_pacing_flag_and_zero_schedule_bound_fraction below
        together cover "closed mode unchanged".
        """
        session = _two_turn_session()
        client = _FakeClient(plan=[(0.05, 0.5, 100), (0.05, 0.5, 50)])
        results = await run.run_session(
            client,
            "http://fake",
            session,
            asyncio.Semaphore(1),
            start_delay_s=0.0,
            schedule=None,
            run_started=None,
        )
        self.assertEqual(len(results), 2)
        self.assertIsNone(results[0].schedule_bound)
        self.assertIsNone(results[1].schedule_bound)
        self.assertIsNone(results[0].scheduled_send_ts)
        self.assertIsNone(results[1].scheduled_send_ts)
        self.assertIsNone(results[0].scheduled_admission_delay_s)
        self.assertIsNone(results[1].scheduled_admission_delay_s)
        expected_ready_time = results[0].latency_s + session.turns[1].think_time_before_s
        self.assertAlmostEqual(client.send_times[1], expected_ready_time, places=6)


class _VirtualClock:
    """A deterministic virtual clock for driving many concurrent asyncio tasks.

    SendTimeRuleTest's fake clock (above) advances "now" by the sleep's own
    delay on every call, which is only correct when a single task is ever
    sleeping at a time. With N genuinely concurrent sessions (this file's
    admission-queue end-to-end tests below), that naive clock is wrong: if
    task A sleeps 5s and task B concurrently sleeps 2s, "now" must advance to
    the earliest of the two wake times, not their sum.

    Implemented as the standard virtual-time technique for pure-asyncio code
    that only ever blocks via ``asyncio.sleep`` and ``asyncio.Semaphore``
    (true here: the fake HTTP client below never does real I/O): track how
    many of the still-running tasks are currently blocked -- either sleeping
    (registered in ``_heap``) or waiting to acquire the semaphore (see
    ``_TrackedSemaphore``, which does not go through ``_heap`` since a slot
    can free up without any time passing). Once every still-running,
    not-semaphore-waiting task is asleep, advance "now" to the earliest
    pending wake time and resolve it (and any ties). A session waiting on the
    semaphore is deliberately excluded from that count: it becomes
    runnable only when another session's real ``async with semaphore:`` block
    exits, a same-instant event that needs no virtual time to elapse.
    """

    def __init__(self, active_tasks: int) -> None:
        self.now = 0.0
        self._active = active_tasks
        self._waiting_on_semaphore = 0
        self._heap: list[tuple[float, int, asyncio.Future]] = []
        self._seq = 0

    def perf_counter(self) -> float:
        return self.now

    def task_done(self) -> None:
        self._active -= 1
        self._maybe_advance()

    def semaphore_wait_started(self) -> None:
        self._waiting_on_semaphore += 1
        self._maybe_advance()

    def semaphore_wait_ended(self) -> None:
        self._waiting_on_semaphore -= 1

    def _maybe_advance(self) -> None:
        blockable = self._active - self._waiting_on_semaphore
        if not self._heap or len(self._heap) < blockable:
            return
        self.now = max(self.now, self._heap[0][0])
        while self._heap and self._heap[0][0] <= self.now:
            _, _, future = heapq.heappop(self._heap)
            if not future.done():
                future.set_result(None)

    async def sleep(self, delay: float | None, result: object = None) -> object:
        if not delay or delay <= 0:
            await _REAL_ASYNCIO_SLEEP(0)
            return result
        future = asyncio.get_event_loop().create_future()
        self._seq += 1
        heapq.heappush(self._heap, (self.now + delay, self._seq, future))
        self._maybe_advance()
        await future
        return result


_REAL_ASYNCIO_SLEEP = asyncio.sleep


class _TrackedSemaphore(asyncio.Semaphore):
    """A semaphore whose acquire-wait is visible to a ``_VirtualClock``.

    A task blocked here is not "asleep" from the clock's point of view: it
    only becomes runnable when another task's ``release()`` (its ``async
    with`` block exiting) hands it the slot, not because virtual time passed.
    """

    def __init__(self, value: int, clock: _VirtualClock) -> None:
        super().__init__(value)
        self._clock = clock

    async def acquire(self) -> bool:
        self._clock.semaphore_wait_started()
        try:
            return await super().acquire()
        finally:
            self._clock.semaphore_wait_ended()


class _SpeedFakeResponse(_FakeResponse):
    """Alias for readability at the call sites below."""


class _SpeedFakeClient:
    """A fake HTTP client whose response speed is a fixed (ttft_s, tpot_s).

    Unlike ``_FakeClient`` (a fixed reply plan popped in call order), this
    computes each reply from the request's own ``max_tokens``, so it works
    correctly when many sessions issue requests concurrently and interleaved.
    ``n_tokens`` is exactly ``max_tokens``: decoding is greedy with
    ``ignore_eos`` (see run.py's module docstring), so the server always
    fills the requested budget.
    """

    def __init__(self, ttft_s: float, tpot_s: float) -> None:
        self._ttft_s = ttft_s
        self._tpot_s = tpot_s

    def post(self, url: str, json: dict, timeout: object) -> _SpeedFakeResponse:  # noqa: A002
        max_tokens = json["max_tokens"]
        total_s = self._ttft_s + self._tpot_s * (max_tokens - 1)
        return _SpeedFakeResponse(self._ttft_s, total_s, max_tokens)


async def _run_all_sessions_with_virtual_clock(
    sessions: list[run.Session],
    start_delays: list[float],
    schedule: dict[int, tuple[float, ...]],
    concurrency: int,
    client: _SpeedFakeClient,
) -> list[run.TurnResult]:
    """Run every session concurrently under a _VirtualClock and return all results."""
    clock = _VirtualClock(len(sessions))

    async def run_and_finish(session: run.Session, index: int) -> list[run.TurnResult]:
        try:
            return await run.run_session(
                client,
                "http://fake",
                session,
                semaphore,
                start_delay_s=start_delays[index],
                schedule=schedule[session.session_id],
                run_started=0.0,
            )
        finally:
            clock.task_done()

    with (
        mock.patch("time.perf_counter", new=clock.perf_counter),
        mock.patch("asyncio.sleep", new=clock.sleep),
    ):
        semaphore = _TrackedSemaphore(concurrency, clock)
        tasks = [
            asyncio.create_task(run_and_finish(session, index))
            for index, session in enumerate(sessions)
        ]
        per_session_results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=30.0)
    return [result for results in per_session_results for result in results]


@unittest.skipIf(run is None, f"could not import run.py: {_IMPORT_ERROR}")
class AdmissionQueueEndToEndTest(unittest.TestCase):
    """schedule_bound_fraction on the real 48-session/CONCURRENCY=16 workload.

    Exercises run_session for every session at once (through the real
    CONCURRENCY-slot semaphore) under a _VirtualClock, so these are true
    concurrency tests, not single-session extrapolations. Plain TestCase +
    asyncio.run() rather than IsolatedAsyncioTestCase: the latter forces
    asyncio debug mode (asyncio.Runner(debug=True) -- see
    unittest.async_case._setupAsyncioRunner), which adds per-callback
    overhead that is negligible for a couple of awaits but adds up over the
    tens of thousands of per-token sleeps this workload's full 48 sessions
    generate, turning a sub-second test into one that takes 10+ seconds.
    """

    def test_fast_server_is_almost_entirely_schedule_bound(self) -> None:
        asyncio.run(self._fast_server_is_almost_entirely_schedule_bound())

    async def _fast_server_is_almost_entirely_schedule_bound(self) -> None:
        # A server far faster than the reference (ttft=10ms, tpot=1ms) always
        # finishes long before its scheduled time, whether or not it queued
        # for admission: this is the fixed behavior job 632584 was missing,
        # where a fast server's offered load stayed coupled to its own speed.
        sessions = run.generate_sessions(run.SEED, run.N_SESSIONS)
        start_delays = run.session_start_delays(len(sessions))
        schedule = run.compute_schedule(sessions, start_delays, REF_TTFT_MS, REF_TPOT_MS)
        client = _SpeedFakeClient(ttft_s=0.01, tpot_s=0.001)
        results = await _run_all_sessions_with_virtual_clock(
            sessions, start_delays, schedule, run.CONCURRENCY, client
        )
        values = run.aggregate_metrics(
            results,
            wall_s=1.0,
            pacing="scheduled",
            ref_ttft_ms=REF_TTFT_MS,
            ref_tpot_ms=REF_TPOT_MS,
        )
        self.assertGreater(values["schedule_bound_fraction"], 0.95)

    def test_reference_speed_exactly_produces_a_mix(self) -> None:
        asyncio.run(self._reference_speed_exactly_produces_a_mix())

    async def _reference_speed_exactly_produces_a_mix(self) -> None:
        # A server running at exactly the assumed reference speed should
        # track the schedule closely rather than degrade wholesale to
        # closed-loop: some turns land on either side of their scheduled
        # instant, so schedule_bound is neither uniformly True nor uniformly
        # False.
        sessions = run.generate_sessions(run.SEED, run.N_SESSIONS)
        start_delays = run.session_start_delays(len(sessions))
        schedule = run.compute_schedule(sessions, start_delays, REF_TTFT_MS, REF_TPOT_MS)
        client = _SpeedFakeClient(ttft_s=REF_TTFT_MS / 1000.0, tpot_s=REF_TPOT_MS / 1000.0)
        results = await _run_all_sessions_with_virtual_clock(
            sessions, start_delays, schedule, run.CONCURRENCY, client
        )
        values = run.aggregate_metrics(
            results,
            wall_s=1.0,
            pacing="scheduled",
            ref_ttft_ms=REF_TTFT_MS,
            ref_tpot_ms=REF_TPOT_MS,
        )
        self.assertGreater(values["schedule_bound_fraction"], 0.0)
        self.assertLess(values["schedule_bound_fraction"], 1.0)

    def test_deterministic_across_runs(self) -> None:
        asyncio.run(self._deterministic_across_runs())

    async def _deterministic_across_runs(self) -> None:
        sessions = run.generate_sessions(run.SEED, run.N_SESSIONS)
        start_delays = run.session_start_delays(len(sessions))
        schedule = run.compute_schedule(sessions, start_delays, REF_TTFT_MS, REF_TPOT_MS)
        client = _SpeedFakeClient(ttft_s=0.01, tpot_s=0.001)
        first = await _run_all_sessions_with_virtual_clock(
            sessions, start_delays, schedule, run.CONCURRENCY, client
        )
        second = await _run_all_sessions_with_virtual_clock(
            sessions, start_delays, schedule, run.CONCURRENCY, client
        )
        key = lambda r: (r.session_id, r.turn_index)  # noqa: E731
        self.assertEqual(
            {key(r): r.schedule_bound for r in first},
            {key(r): r.schedule_bound for r in second},
        )


@unittest.skipIf(run is None, f"could not import run.py: {_IMPORT_ERROR}")
class AggregateMetricsTest(unittest.TestCase):
    """aggregate_metrics(): the new fields, and schedule_bound_fraction's definition."""

    def _turn(self, **kwargs) -> run.TurnResult:
        defaults = dict(
            session_id=0,
            turn_index=1,
            ok=True,
            ttft_s=0.01,
            completion_tokens=10,
            latency_s=0.5,
        )
        defaults.update(kwargs)
        return run.TurnResult(**defaults)

    def test_new_fields_present_and_finite(self) -> None:
        results = [
            self._turn(turn_index=1, schedule_bound=None),
            self._turn(turn_index=2, schedule_bound=True),
            self._turn(turn_index=2, schedule_bound=False),
        ]
        values = run.aggregate_metrics(
            results,
            wall_s=2.0,
            pacing="scheduled",
            ref_ttft_ms=700.0,
            ref_tpot_ms=110.0,
        )
        for key in run.METRICS:
            self.assertIn(key, values)
            self.assertTrue(math.isfinite(values[key]), f"{key} = {values[key]!r} is not finite")
        self.assertEqual(values["benchmark_version"], 4.0)
        self.assertEqual(values["pacing_scheduled"], 1.0)
        self.assertEqual(values["ref_ttft_ms"], 700.0)
        self.assertEqual(values["ref_tpot_ms"], 110.0)
        self.assertEqual(values["concurrency"], float(run.CONCURRENCY))
        self.assertAlmostEqual(values["schedule_bound_fraction"], 0.5)
        self.assertAlmostEqual(values["offered_turn_rate_per_s"], 3 / 2.0)
        self.assertEqual(values["wall_duration_s"], 2.0)

    def test_closed_pacing_flag_and_zero_schedule_bound_fraction(self) -> None:
        results = [
            self._turn(turn_index=1, schedule_bound=None),
            self._turn(turn_index=2, schedule_bound=None),
        ]
        values = run.aggregate_metrics(
            results, wall_s=1.0, pacing="closed", ref_ttft_ms=700.0, ref_tpot_ms=110.0
        )
        self.assertEqual(values["pacing_scheduled"], 0.0)
        self.assertEqual(values["schedule_bound_fraction"], 0.0)

    def test_schedule_bound_fraction_ignores_turn_one(self) -> None:
        # Turn 1 is never schedule-bound; it must not affect the denominator.
        results = [
            self._turn(turn_index=1, schedule_bound=None),
            self._turn(turn_index=2, schedule_bound=True),
        ]
        values = run.aggregate_metrics(
            results,
            wall_s=1.0,
            pacing="scheduled",
            ref_ttft_ms=700.0,
            ref_tpot_ms=110.0,
        )
        self.assertEqual(values["schedule_bound_fraction"], 1.0)


if __name__ == "__main__":
    unittest.main()
