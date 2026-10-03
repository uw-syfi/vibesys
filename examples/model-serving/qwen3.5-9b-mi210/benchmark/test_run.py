#!/usr/bin/env python3
"""Unit tests for run.py's trace resolution: modes, digests, and warmup disjointness.

No server, no GPU, no session_runner binary: these exercise pure path
resolution and the checked-in trace files themselves. Run with:

    cd examples/model-serving/qwen3.5-9b-mi210 && python3 -m pytest benchmark/test_run.py -q

or ``python3 benchmark/test_run.py``.
"""

from __future__ import annotations

import argparse
import csv
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from hypothesis import assume, given, settings
from hypothesis import strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run


def _session_ids(path: Path) -> set[str]:
    with path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        return {row["session_id"] for row in reader}


class ModeTableTests(unittest.TestCase):
    def test_holdout_is_a_valid_mode(self) -> None:
        self.assertIn("holdout", run.MODE_SESSIONS)

    def test_holdout_session_count_matches_full(self) -> None:
        # Same shape/scale as full, per README.md "Held-out evaluation".
        self.assertEqual(run.MODE_SESSIONS["holdout"], run.MODE_SESSIONS["full"])

    def test_mode_flag_accepts_holdout(self) -> None:
        args = run.parse_args(["--mode", "holdout", "--request-factory-engine", "/bin/true"])
        self.assertEqual(args.mode, "holdout")


class TraceDigestTests(unittest.TestCase):
    """The checked-in trace slices must match their pinned digest and row count."""

    def test_default_trace_verifies(self) -> None:
        self.assertEqual(run.verify_default_trace(), run.DEFAULT_TRACE)

    def test_holdout_trace_verifies(self) -> None:
        self.assertEqual(run.verify_holdout_trace(), run.HOLDOUT_TRACE)

    def test_warmup_trace_verifies(self) -> None:
        self.assertEqual(run.verify_warmup_trace(), run.WARMUP_TRACE)

    def test_tampered_trace_is_rejected(self) -> None:
        with self.assertRaises(run.HarnessError):
            run.verify_trace(run.DEFAULT_TRACE, "0" * 64, run.DEFAULT_TRACE_ROWS, label="test")
        with self.assertRaises(run.HarnessError):
            run.verify_trace(run.DEFAULT_TRACE, run.DEFAULT_TRACE_SHA256, 1, label="test")


class SessionRangeDisjointnessTests(unittest.TestCase):
    """Every mode must warm up on sessions no mode ever measures.

    Regression coverage for the fix: before WARMUP_TRACE existed, quick/full
    warmed up on sessions 0-11 of their own measured trace (see README.md
    "Warmup"), so those 12 sessions started the measured sub-run pre-cached.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.warmup_ids = _session_ids(run.WARMUP_TRACE)
        cls.default_ids = _session_ids(run.DEFAULT_TRACE)
        cls.holdout_ids = _session_ids(run.HOLDOUT_TRACE)

    def test_warmup_pool_has_exactly_warmup_sessions_sessions(self) -> None:
        self.assertEqual(len(self.warmup_ids), run.WARMUP_SESSIONS)

    def test_warmup_disjoint_from_quick_full_measured_range(self) -> None:
        self.assertEqual(self.warmup_ids & self.default_ids, set())

    def test_warmup_disjoint_from_holdout_measured_range(self) -> None:
        # holdout measures only the first MODE_SESSIONS["holdout"] sessions of
        # HOLDOUT_TRACE (see resolve_trace/run_replay); the warmup pool must be
        # disjoint from that measured prefix specifically, and in this case
        # from the whole checked-in file (WARMUP_TRACE's sessions never appear
        # in HOLDOUT_TRACE at all).
        self.assertEqual(self.warmup_ids & self.holdout_ids, set())

    def test_quick_full_and_holdout_measured_ranges_are_disjoint(self) -> None:
        self.assertEqual(self.default_ids & self.holdout_ids, set())

    def test_every_mode_warmup_and_measured_session_ids_are_disjoint(self) -> None:
        # The general property the coordinator asked to lock down: for every
        # real (non-smoke) mode, the set of sessions its warmup sub-run can
        # touch and the set its measured sub-run can touch never intersect.
        measured_by_mode = {
            "quick": self.default_ids,  # first 60 of DEFAULT_TRACE; superset used here is fine for a disjointness check
            "full": self.default_ids,
            "holdout": self.holdout_ids,
        }
        for mode, measured_ids in measured_by_mode.items():
            with self.subTest(mode=mode):
                self.assertEqual(self.warmup_ids & measured_ids, set())


class ResolveTraceTests(unittest.TestCase):
    def _args(self, trace=None) -> argparse.Namespace:
        return argparse.Namespace(trace=trace)

    def test_quick_defaults_to_the_checked_in_default_trace(self) -> None:
        self.assertEqual(run.resolve_trace(self._args(), "quick"), run.DEFAULT_TRACE)

    def test_full_defaults_to_the_checked_in_default_trace(self) -> None:
        self.assertEqual(run.resolve_trace(self._args(), "full"), run.DEFAULT_TRACE)

    def test_holdout_defaults_to_the_checked_in_holdout_trace(self) -> None:
        self.assertEqual(run.resolve_trace(self._args(), "holdout"), run.HOLDOUT_TRACE)

    def test_explicit_trace_wins_for_every_mode(self) -> None:
        # Any real file works here: _configured only checks existence, it
        # does not validate shape (that is the caller's job -- see
        # resolve_trace's docstring on override responsibility).
        explicit = run.WARMUP_TRACE
        for mode in ("quick", "full", "holdout"):
            with self.subTest(mode=mode):
                self.assertEqual(run.resolve_trace(self._args(explicit), mode), explicit)


class ResolvePathsWarmupTraceTests(unittest.TestCase):
    """resolve_paths always attaches the checked-in, verified warmup trace."""

    def _args(self, tmp_tokenizer: Path) -> argparse.Namespace:
        return argparse.Namespace(
            trace=None,
            text_file=Path(__file__).resolve().parent.parent
            / "benchmark"
            / "traces"
            / "coding_session_0000-0259.csv",  # any real file; corpus/tokenizer are independently resolved below
            tokenizer=tmp_tokenizer,
            model=run.DEFAULT_MODEL,
        )

    def test_warmup_trace_is_always_the_checked_in_pool(self) -> None:
        # Use --text-file/--tokenizer explicitly so this test needs neither
        # network access (fetch_corpus) nor a local HF cache.
        args = self._args(Path(__file__))
        for mode in ("quick", "full", "holdout"):
            with self.subTest(mode=mode):
                paths = run.resolve_paths(args, mode)
                self.assertEqual(paths.warmup_trace, run.WARMUP_TRACE)


FAKE_HANG_RUNNER = textwrap.dedent(
    """\
    #!{python}
    import sys, time
    print("session workload | sessions=12 rounds=72 max_prompt_len=9000 max_prefix_len=8000 "
          "max_input_len=3000 max_output_len=900 total_output_len=14343 max_arrival_time_ms=0.000 "
          "total_tool_wait_after_ms=0.000", file=sys.stderr, flush=True)
    time.sleep(3600)  # killed by the harness's limit long before this returns
    """
)

# Header, then progress lines as session_runner prints them every 5 s (the status
# line it also prints every 500 ms is included to show it does not confuse the parse).
FAKE_PROGRESS_RUNNER = textwrap.dedent(
    """\
    #!{python}
    import sys, time
    def err(line):
        print(line, file=sys.stderr, flush=True)
    err("session workload | sessions=12 rounds=72 max_prompt_len=9000 max_prefix_len=8000 "
        "max_input_len=3000 max_output_len=900 total_output_len=14343 max_arrival_time_ms=0.000 "
        "total_tool_wait_after_ms=0.000")
    err("progress | elapsed_s=5.0 rounds_done=3/72 sessions_done=0/12 output_tokens=610")
    err("progress | elapsed_s=10.0 rounds_done=9/72 sessions_done=1/12 output_tokens=1800")
    err("sessions 1/12 | steps 9/72 completed=9 submitted=12 active=3 failed=0 "
        "runtime_global_queue_depth=0 | elapsed=10.5s")
    time.sleep(3600)
    """
)


class SessionRunnerTimeoutTests(unittest.TestCase):
    """A killed session_runner reports the job size and needed rate before the command."""

    def _run_hanging(self, script_body: str, timeout_s: float) -> str:
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "fake_session_runner"
            engine.write_text(script_body.format(python=sys.executable))
            engine.chmod(0o755)
            with self.assertRaises(run.HarnessError) as caught:
                run.run_session_runner(
                    engine, ["--trace", "t.csv"], timeout_s=timeout_s, label="warmup sub-run"
                )
        return str(caught.exception)

    def test_timeout_message_reports_the_last_progress_line_then_the_command(self) -> None:
        message = self._run_hanging(FAKE_PROGRESS_RUNNER, timeout_s=2.0)
        headline, _, command_line = message.partition("\n")
        self.assertIn("warmup sub-run timed out after 2s", headline)
        self.assertIn("(at 10s): 9/72 rounds, 1/12 sessions, 1800 output tokens", headline)
        self.assertIn("180.0 output tokens/s achieved", headline)
        self.assertIn("14343 output tokens within the 2s limit needs 7171.5", headline)
        self.assertNotIn("610", headline)
        self.assertTrue(command_line.startswith("command: "))

    def test_timeout_with_only_the_workload_header_falls_back_to_the_needed_rate(self) -> None:
        message = self._run_hanging(FAKE_HANG_RUNNER, timeout_s=2.0)
        headline, _, command_line = message.partition("\n")
        self.assertIn("warmup sub-run timed out after 2s", headline)
        self.assertIn("72 rounds (12 sessions, 14343 output tokens)", headline)
        self.assertIn("at least 7171.5 output tokens/s", headline)
        self.assertIn("printed no progress line", headline)
        self.assertNotIn("--trace", headline)
        self.assertTrue(command_line.startswith("command: "))
        self.assertIn("--trace t.csv", command_line)

    def test_timeout_without_workload_header_says_progress_is_unknown(self) -> None:
        silent = "#!{python}\nimport time\ntime.sleep(3600)\n"
        message = self._run_hanging(silent, timeout_s=0.5)
        self.assertIn("Progress is unknown", message.splitlines()[0])
        self.assertIn("command: ", message)


# r13 warmup header: 12 sessions, 72 rounds, 14343 output tokens.
WARMUP_HEADER = (
    "session workload | sessions=12 rounds=72 max_prompt_len=9000 max_prefix_len=8000 "
    "max_input_len=3000 max_output_len=900 total_output_len=14343 max_arrival_time_ms=0.000 "
    "total_tool_wait_after_ms=0.000"
)
WARMUP_TOTAL = 14343


def _progress(elapsed: float, rounds: int, sessions_done: int, tokens: int) -> str:
    return (
        f"progress | elapsed_s={elapsed:.1f} rounds_done={rounds}/72 "
        f"sessions_done={sessions_done}/12 output_tokens={tokens}"
    )


def _warmup_watch() -> run.WarmupWatch:
    watch = run.WarmupWatch(180.0, run.WARMUP_SESSION_CEILING_TOK_S, label="warmup sub-run")
    assert watch.feed(WARMUP_HEADER) is None
    return watch


def _first_stop(watch: run.WarmupWatch, lines: list[str]) -> tuple[int, str] | None:
    for i, line in enumerate(lines):
        if (why := watch.feed(line)) is not None:
            return i, why
    return None


class WarmupWatchTests(unittest.TestCase):
    def test_an_r13_rate_is_stopped_once_the_ceiling_cannot_close_the_gap(self) -> None:
        # 17 output tokens/s, 0 sessions done: 12 sessions x 170 tokens/s closes a gap of
        # 14343 - 17t tokens only while 180 - t >= (14343 - 17t) / 2040, i.e. t <= 173.1 s.
        lines = [_progress(t, t // 12, 0, 17 * t) for t in range(5, 180, 5)]
        stop = _first_stop(_warmup_watch(), lines)

        assert stop is not None
        index, message = stop
        self.assertEqual(lines[index], _progress(175, 14, 0, 2975))
        self.assertIn("warmup sub-run stopped at 175s", message)
        self.assertIn("14/72 rounds, 0/12 sessions, 2975 output tokens", message)
        self.assertIn("17.0 output tokens/s achieved", message)
        self.assertIn("needs 79.7 output tokens/s on average", message)
        self.assertIn("above the 2040 that 12 unfinished sessions", message)

    def test_finished_sessions_lower_the_ceiling(self) -> None:
        watch = _warmup_watch()
        # 6 sessions left at 150 s with 9000 tokens: 6 x 170 x 30 = 30600 >= 5343, keep going.
        self.assertIsNone(watch.feed(_progress(150, 50, 6, 9000)))
        # 11 sessions done at 179 s: 1 x 170 x 1 = 170 < 343 tokens still to go.
        self.assertIn("1 unfinished sessions", watch.feed(_progress(179, 70, 11, 14000)) or "")

    def test_progress_before_the_workload_header_never_stops(self) -> None:
        watch = run.WarmupWatch(180.0, run.WARMUP_SESSION_CEILING_TOK_S, label="warmup")
        self.assertIsNone(watch.feed(_progress(179, 1, 0, 1)))

    @settings(max_examples=300, deadline=None)
    @given(data=st.data())
    def test_a_run_that_finishes_within_the_limit_is_never_stopped(self, data) -> None:
        # Any stream that respects the ceiling: in each interval, each session unfinished
        # at its start receives at most the ceiling rate. Its last line is the finish, at or
        # before 180 s, so its token count is the workload total.
        ceiling = run.WARMUP_SESSION_CEILING_TOK_S
        times = sorted(
            data.draw(
                st.lists(
                    st.floats(min_value=0.1, max_value=180.0), min_size=1, max_size=40, unique=True
                )
            )
        )
        lines_state = []
        tokens, done, previous = 0, 0, 0.0
        for t in times:
            budget = (12 - done) * ceiling * (t - previous)
            tokens += int(data.draw(st.floats(min_value=0.0, max_value=1.0)) * budget)
            done = data.draw(st.integers(min_value=done, max_value=12))
            lines_state.append((t, done, tokens))
            previous = t
        assume(tokens > 0)  # a workload has output tokens
        total = tokens
        watch = run.WarmupWatch(180.0, ceiling, label="warmup")
        header = WARMUP_HEADER.replace(
            f"total_output_len={WARMUP_TOTAL}", f"total_output_len={total}"
        )
        assert watch.feed(header) is None
        for t, done, tokens in lines_state:
            line = (
                f"progress | elapsed_s={t:.3f} rounds_done=0/72 sessions_done={done}/12 "
                f"output_tokens={tokens}"
            )
            self.assertIsNone(watch.feed(line), line)

    def test_a_stopped_warmup_kills_session_runner_and_reports_why(self) -> None:
        script = textwrap.dedent(
            """\
            #!{python}
            import sys, time
            print({header!r}, file=sys.stderr, flush=True)
            print({line!r}, file=sys.stderr, flush=True)
            time.sleep(3600)
            """
        ).format(python=sys.executable, header=WARMUP_HEADER, line=_progress(178, 15, 0, 3000))
        with tempfile.TemporaryDirectory() as tmp:
            engine = Path(tmp) / "fake_session_runner"
            engine.write_text(script)
            engine.chmod(0o755)
            watch = _warmup_watch()
            with self.assertRaises(run.HarnessError) as caught:
                run.run_session_runner(
                    engine, ["--trace", "t.csv"], timeout_s=600.0, label="warmup", watch=watch.feed
                )
        headline, _, command_line = str(caught.exception).partition("\n")
        self.assertIn("warmup sub-run stopped at 178s", headline)
        self.assertEqual(command_line, f"command: {engine} --trace t.csv")


if __name__ == "__main__":
    unittest.main()
