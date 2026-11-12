"""Unit tests for the sweep logic in replay.py: python3 -I -m pytest, or python3 -I -m unittest."""

from __future__ import annotations

import argparse
import contextlib
import copy
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay  # noqa: E402  # lint-waiver: LW-731552 [E402]; replay.py is a standalone script beside this test, importable only once its directory is on sys.path.


def summary(**window: object) -> dict:
    base_window = {
        "warmup_s": 60,
        "duration_s": 180,
        "output_tokens": 90_000,
        "requests_submitted": 500,
        "requests_first_token": 480,
        "ttft_censored_requests": 20,
        "requests_cancelled_at_window_end": 30,
        "requests_failed": 0,
        "requests_output_length_mismatch": 0,
        "warmup_requests_failed": 0,
        "sessions_started": 3,
        "requests_without_token_ids": 0,
    }
    return {
        "replay": {
            "kind": "sessions",
            "common": {
                "failed_steps": 0,
                "output_token_throughput_per_s": 500.0,
                "ttft_ms_p50": 1200.0,
                "ttft_ms_p90": 9000.0,
                "tpot_ms_p50": 30.0,
                "tpot_ms_p90": 60.0,
            },
            "window": base_window | window,
        }
    }


class StepMetrics(unittest.TestCase):
    def test_valid_window(self) -> None:
        self.assertEqual(
            replay.step_metrics(summary(), 60, 180),
            {
                "output_tokens_per_s": 500.0,
                "ttft_p50_ms": 1200.0,
                "ttft_p90_ms": 9000.0,
                "tpot_p50_ms": 30.0,
                "tpot_p90_ms": 60.0,
            },
        )

    def test_each_failure_signal_is_rejected(self) -> None:
        for key in (
            "requests_failed",
            "warmup_requests_failed",
            "requests_output_length_mismatch",
            "requests_without_token_ids",
        ):
            with self.subTest(key), self.assertRaises(ValueError):
                replay.step_metrics(summary(**{key: 1}), 60, 180)
        failed = summary()
        failed["replay"]["common"]["failed_steps"] = 2
        with self.assertRaises(ValueError):
            replay.step_metrics(failed, 60, 180)

    def test_empty_or_unwindowed_or_mismatched_window_is_rejected(self) -> None:
        for bad in (summary(output_tokens=0), summary(output_tokens=-1), summary(warmup_s=30)):
            with self.assertRaises(ValueError):
                replay.step_metrics(bad, 60, 180)
        unwindowed = summary()
        del unwindowed["replay"]["window"]
        with self.assertRaises(ValueError):
            replay.step_metrics(unwindowed, 60, 180)

    def test_missing_nonfinite_or_negative_metrics_are_rejected(self) -> None:
        for field in replay.METRICS.values():
            for value in (None, float("nan"), float("inf"), -1.0, True, "1"):
                bad = copy.deepcopy(summary())
                if value is None:
                    del bad["replay"]["common"][field]
                else:
                    bad["replay"]["common"][field] = value
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    replay.step_metrics(bad, 60, 180)

    def test_window_with_no_submissions_censors_latency_at_its_length(self) -> None:
        idle = copy.deepcopy(summary())
        idle["replay"]["window"]["requests_submitted"] = 0
        for key in ("ttft_ms_p50", "ttft_ms_p90"):
            idle["replay"]["common"][key] = None
        del idle["replay"]["common"]["tpot_ms_p90"]
        values = replay.step_metrics(idle, 60, 180)
        self.assertEqual(values["ttft_p50_ms"], 180_000.0)
        self.assertEqual(values["ttft_p90_ms"], 180_000.0)
        self.assertEqual(values["tpot_p90_ms"], 180_000.0)
        self.assertFalse(replay.passes_cap(values))
        idle["replay"]["common"]["output_token_throughput_per_s"] = None
        with self.assertRaises(ValueError):
            replay.step_metrics(idle, 60, 180)

    def test_zero_throughput_and_malformed_summaries_are_rejected(self) -> None:
        zero = summary()
        zero["replay"]["common"]["output_token_throughput_per_s"] = 0.0
        for bad in (zero, None, {}, {"replay": {"kind": "independent"}}):
            with self.assertRaises(ValueError):
                replay.step_metrics(bad, 60, 180)

    def test_counts_must_be_integers(self) -> None:
        for value in (None, 0.0, True, "0"):
            with self.assertRaises(ValueError):
                replay.step_metrics(summary(requests_failed=value), 60, 180)


def row(tput: float, ttft: float = 1000.0, tpot: float = 30.0) -> dict[str, float]:
    return {
        "output_tokens_per_s": tput,
        "ttft_p50_ms": ttft,
        "ttft_p90_ms": ttft * 2,
        "tpot_p50_ms": tpot,
        "tpot_p90_ms": tpot * 2,
    }


def drive(tputs: dict[int, float | dict], levels=replay.CONCURRENCIES) -> list[int]:
    """Run the planner against canned rows; a number is a passing row, a dict a full row.

    Concurrencies not in the table (e.g. refinement steps) get the value of `default`.
    """
    history: dict[int, dict] = {}
    order: list[int] = []
    while (c := replay.plan_next(levels, history)) is not None:
        value = tputs[c]
        history[c] = value if isinstance(value, dict) else row(value)
        order.append(c)
        assert len(order) < 30
    return order


class Cap(unittest.TestCase):
    def test_boundaries_pass(self) -> None:
        self.assertTrue(replay.passes_cap(row(1, ttft=10_000.0, tpot=100.0)))
        self.assertFalse(replay.passes_cap(row(1, ttft=10_000.1)))
        self.assertFalse(replay.passes_cap(row(1, tpot=100.1)))


class Climb(unittest.TestCase):
    def test_no_stop_while_gaining(self) -> None:
        self.assertFalse(replay.climb_stopped([row(100.0)]))
        self.assertFalse(replay.climb_stopped([row(100.0), row(104.0)]))
        self.assertFalse(replay.climb_stopped([row(100.0), row(180.0), row(190.0)]))

    def test_stop_below_three_percent_gain(self) -> None:
        self.assertTrue(replay.climb_stopped([row(100.0), row(102.9)]))
        self.assertTrue(replay.climb_stopped([row(100.0), row(90.0)]))
        # Against the best so far, not the previous step.
        self.assertTrue(replay.climb_stopped([row(100.0), row(200.0), row(150.0)]))
        self.assertTrue(replay.climb_stopped([row(100.0), row(200.0), row(205.0)]))

    def test_exactly_three_percent_continues(self) -> None:
        self.assertFalse(replay.climb_stopped([row(100.0), row(103.0)]))

    def test_failing_the_cap_stops_even_with_a_gain(self) -> None:
        self.assertTrue(replay.climb_stopped([row(100.0), row(200.0, tpot=101.0)]))
        self.assertTrue(replay.climb_stopped([row(100.0), row(200.0, ttft=20_000.0)]))
        self.assertTrue(replay.climb_stopped([row(100.0, tpot=150.0)]))

    def test_full_climb_runs_every_level(self) -> None:
        tputs = {c: 100.0 * (i + 1) for i, c in enumerate(replay.CONCURRENCIES)}
        self.assertEqual(drive(tputs), list(replay.CONCURRENCIES))  # p=256: no 2p, no refinement

    def test_climb_stops_above_the_fixed_levels(self) -> None:
        # 4, 8, 16, 32 gain; 64 does not: the climb stops at 64, p=32 and 2p=64 ran: refine to 45.
        tputs = {4: 100, 8: 200, 16: 400, 32: 800, 64: 790, 45: 700}
        self.assertEqual(drive(tputs), [4, 8, 16, 32, 64, 45])

    def test_latency_cap_stops_the_climb(self) -> None:
        # 32 fails the cap, so the climb stops; p=16 and 2p=32 ran: refinement 23.
        tputs = {4: 100, 8: 200, 16: 400, 32: row(800, tpot=120.0), 23: 300}
        self.assertEqual(drive(tputs), [4, 8, 16, 32, 23])


class FixedLevels(unittest.TestCase):
    def test_level_4_always_runs_and_starts_the_climb(self) -> None:
        # 4 fails the cap: the climb stops there and nothing else runs.
        self.assertEqual(drive({4: row(100, tpot=500.0)}), [4])

    def test_climb_can_stop_at_8(self) -> None:
        # 8 gains under 3%: the climb stops; p=8 is best and 2p=16 never ran: no refinement.
        self.assertEqual(drive({4: 100, 8: 101}), [4, 8])

    def test_climb_stopped_at_16_runs_nothing_above(self) -> None:
        self.assertEqual(drive({4: 100, 8: 200, 16: 201}), [4, 8, 16])

    def test_all_failing_runs_exactly_the_fixed_prefix(self) -> None:
        bad = row(10, tpot=999.0)
        self.assertEqual(drive({4: bad, 8: bad, 16: bad}), [4])


class Refinement(unittest.TestCase):
    def test_rounded_values(self) -> None:
        self.assertEqual(replay.refinement_candidates(), (6, 11, 23, 45, 91, 181))
        for p, expected in ((4, 6), (8, 11), (16, 23), (32, 45), (64, 91), (128, 181)):
            history = {p: row(100.0), 2 * p: row(90.0)}
            self.assertEqual(replay.refinement_concurrency(history), expected)

    def test_needs_both_p_and_2p(self) -> None:
        self.assertIsNone(replay.refinement_concurrency({16: row(100.0), 8: row(50.0)}))
        self.assertIsNone(replay.refinement_concurrency({256: row(100.0)}))

    def test_uses_the_best_passing_step_not_the_best_overall(self) -> None:
        history = {16: row(100.0), 32: row(300.0, tpot=200.0)}
        self.assertEqual(replay.refinement_concurrency(history), 23)  # p=16, 2p=32 ran
        history = {16: row(100.0), 32: row(90.0), 64: row(500.0, tpot=200.0)}
        self.assertEqual(replay.refinement_concurrency(history), 23)

    def test_none_without_a_passing_step_and_once_run(self) -> None:
        self.assertIsNone(replay.refinement_concurrency({4: row(1, tpot=999.0), 8: row(1)}))
        # (8 passes in the second: p=8, 2p=16 not run)
        self.assertIsNone(
            replay.refinement_concurrency({16: row(100.0), 32: row(90.0), 23: row(95.0)})
        )

    def test_refinement_is_a_peak_candidate(self) -> None:
        history = {16: row(100.0), 32: row(90.0), 23: row(120.0)}
        self.assertEqual(replay.choose_peak(history), (120.0, 23, True))
        self.assertIsNone(
            replay.plan_next(replay.CONCURRENCIES[:5], {**history, 4: row(1), 8: row(2)})
        )

    def test_refinement_comes_after_the_climb_and_is_only_one_step(self) -> None:
        tputs = {4: 100, 8: 200, 16: 400, 32: 390, 23: 500}
        self.assertEqual(drive(tputs), [4, 8, 16, 32, 23])


class Peak(unittest.TestCase):
    def test_peak_is_highest_passing_throughput(self) -> None:
        self.assertEqual(
            replay.choose_peak({4: row(100), 8: row(200), 16: row(150)}), (200, 8, True)
        )

    def test_failing_steps_are_not_candidates(self) -> None:
        history = {4: row(100), 8: row(300, tpot=150.0), 16: row(120)}
        self.assertEqual(replay.choose_peak(history), (120, 16, True))

    def test_lowest_concurrency_within_three_percent(self) -> None:
        history = {4: row(100), 8: row(197), 16: row(200), 32: row(150)}
        self.assertEqual(replay.choose_peak(history), (200, 8, True))  # 197 >= 0.97 * 200
        history[8] = row(193.9)  # below 194
        self.assertEqual(replay.choose_peak(history), (200, 16, True))

    def test_a_failing_step_is_not_the_lowest_within_tolerance(self) -> None:
        history = {4: row(199, ttft=20_000.0), 8: row(150), 16: row(200)}
        self.assertEqual(replay.choose_peak(history)[1], 16)

    def test_tie_goes_to_lower_concurrency(self) -> None:
        self.assertEqual(replay.choose_peak({8: row(200), 16: row(200)})[1], 8)

    def test_no_passing_step_uses_the_lowest_step(self) -> None:
        history = {8: row(70, tpot=999.0), 4: row(50, ttft=99_999.0), 16: row(90, tpot=300.0)}
        self.assertEqual(replay.choose_peak(history), (50, 4, False))

    def test_empty_is_an_error(self) -> None:
        with self.assertRaises(ValueError):
            replay.choose_peak({})


class Result(unittest.TestCase):
    def test_values(self) -> None:
        history = {
            4: row(100, ttft=1, tpot=5),
            8: row(190, ttft=2, tpot=6),
            16: row(200, ttft=3, tpot=7),
            32: row(205, ttft=4, tpot=8),
            64: row(150, tpot=300.0),
        }
        values = replay.build_result(history)
        self.assertEqual(values["output_tokens_per_s"], 205)
        self.assertEqual(values["peak_concurrency"], 16)  # 190 < 0.97*205, 200 >= 198.85
        self.assertEqual(values["latency_cap_met"], 1)
        self.assertEqual((values["ttft_p50_ms_c4"], values["tpot_p50_ms_c4"]), (1, 5))
        self.assertNotIn("ttft_p50_ms_c16", values)
        self.assertEqual(values["tpot_p90_ms_c4"], 10)
        self.assertEqual(
            {k: v for k, v in values.items() if k.startswith("tok_s_at_c")},
            {f"tok_s_at_c{c}": r["output_tokens_per_s"] for c, r in history.items()},
        )
        self.assertTrue(set(values) <= set(replay.HELLO["metrics"]))

    def test_no_passing_step(self) -> None:
        bad = row(10, tpot=999.0)
        values = replay.build_result({4: bad, 8: row(20, tpot=999.0), 16: row(30, tpot=999.0)})
        self.assertEqual(values["output_tokens_per_s"], 10)
        self.assertEqual(values["latency_cap_met"], 0)
        self.assertEqual(values["peak_concurrency"], 4)

    def test_fixed_levels_are_required(self) -> None:
        with self.assertRaises(ValueError):
            replay.build_result({8: row(2), 16: row(3)})

    def test_hello_declares_exactly_three_objectives(self) -> None:
        metrics = replay.HELLO["metrics"]
        required = {n for n, spec in metrics.items() if spec.get("required", True)}
        self.assertEqual(
            required,
            {
                "output_tokens_per_s",
                "ttft_p50_ms_c4",
                "tpot_p50_ms_c4",
            },
        )
        optional = set(metrics) - required
        self.assertLessEqual(
            {
                "peak_concurrency",
                "latency_cap_met",
                "ttft_p90_ms_c4",
                "tpot_p90_ms_c4",
            },
            optional,
        )
        for c in set(replay.CONCURRENCIES) | set(replay.refinement_candidates()):
            self.assertFalse(metrics[f"tok_s_at_c{c}"].get("required", True))

    def test_objectives_toml_matches_the_hello(self) -> None:
        text = (Path(__file__).resolve().parent.parent / "objectives.toml").read_text()
        names = re.findall(r'^name = "([^"]+)"', text, flags=re.M)
        self.assertEqual(names, replay.OBJECTIVES)
        for name in names:
            self.assertIn(name, replay.HELLO["metrics"])
            self.assertTrue(replay.HELLO["metrics"][name].get("required", True))


class Budget(unittest.TestCase):
    def fits(self, elapsed: float, setup: float = 90.0, steps: int = 1) -> bool:
        return replay.next_step_fits(
            elapsed,
            warmup=replay.WARMUP_SECS,
            duration=replay.DURATION_SECS,
            cooldown=replay.COOLDOWN_SECS,
            setup_estimate=setup,
            deadline=replay.DEADLINE_SECS,
            steps=steps,
        )

    def test_boundary(self) -> None:
        step = 15 + 90 + 60 + 180
        self.assertTrue(self.fits(replay.DEADLINE_SECS - step))
        self.assertFalse(self.fits(replay.DEADLINE_SECS - step + 1))
        self.assertTrue(self.fits(replay.DEADLINE_SECS - 2 * step, steps=2))
        self.assertFalse(self.fits(replay.DEADLINE_SECS - 2 * step + 1, steps=2))

    def test_defaults_fit_inside_the_manifest_timeout(self) -> None:
        self.assertEqual(replay.WARMUP_SECS + replay.DURATION_SECS, 240)
        self.assertLess(replay.DEADLINE_SECS, replay.TOTAL_TIMEOUT_SECS)
        # Seven climb steps plus a refinement at the planning setup figure fit the deadline.
        total = 8 * (replay.SETUP_ESTIMATE_SECS + 240 + replay.COOLDOWN_SECS)
        self.assertLess(total, replay.DEADLINE_SECS)
        # A runner process can never be given longer than the whole budget.
        self.assertLess(
            replay.WARMUP_SECS + replay.DURATION_SECS + replay.SETUP_TIMEOUT_SECS,
            replay.TOTAL_TIMEOUT_SECS,
        )

    def test_slow_setup_stops_the_sweep_early(self) -> None:
        self.assertFalse(self.fits(2500, setup=600))


class DeadlinePriority(unittest.TestCase):
    """sweep() with a fake runner: the deadline cuts only the climb above 16 and the refinement."""

    def run_sweep(
        self, deadline: float, tputs: dict[int, float | dict]
    ) -> tuple[list[int], dict | str]:
        calls: list[int] = []
        clock = {"now": 0.0}

        def fake_run_step(args, plan, temporary, concurrency, tokenizer, timeout):
            calls.append(concurrency)
            clock["now"] += plan.setup_estimate + plan.warmup + plan.duration
            value = tputs[concurrency]
            return (value if isinstance(value, dict) else row(value)), {}

        plan = argparse.Namespace(
            concurrencies=replay.CONCURRENCIES,
            warmup=60.0,
            duration=180.0,
            cooldown=15.0,
            deadline=deadline,
            pool_limit=1,
            corpus_lines=1,
            tokenizer="t",
            model="m",
            setup_estimate=90.0,
            setup_timeout=300.0,
        )
        saved = (replay.run_step, replay.time.monotonic, replay.time.sleep, replay._check_trace)

        def sleep(seconds: float) -> None:
            clock["now"] += seconds

        replay.run_step, replay.time.monotonic = fake_run_step, lambda: clock["now"]
        replay.time.sleep, replay._check_trace = sleep, lambda: None
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                try:
                    return calls, replay.sweep(argparse.Namespace(), plan, Path("."))
                except ValueError as exc:
                    return calls, str(exc)
        finally:
            replay.run_step, replay.time.monotonic, replay.time.sleep, replay._check_trace = saved

    STEP = 90 + 240 + 15  # per step, incl. cooldown; the first step has no real cooldown

    def test_deadline_cuts_the_climb(self) -> None:
        tputs = {c: 100.0 * (i + 1) for i, c in enumerate(replay.CONCURRENCIES)}
        calls, result = self.run_sweep(4 * self.STEP + 10, tputs)
        self.assertEqual(calls[:3], [4, 8, 16])
        self.assertEqual(calls[3], 32)  # 4 steps fit; the fifth does not
        self.assertEqual(calls, [4, 8, 16, 32])
        self.assertIsInstance(result, dict)
        self.assertEqual(result["output_tokens_per_s"], 400.0)

    def test_fixed_levels_fit_exactly(self) -> None:
        tputs = {4: 100, 8: 200, 16: 400, 32: 800}
        calls, result = self.run_sweep(3 * self.STEP, tputs)
        self.assertEqual(calls, [4, 8, 16])
        self.assertIsInstance(result, dict)

    def test_unfinishable_fixed_levels_are_an_error(self) -> None:
        calls, result = self.run_sweep(self.STEP - 1, {4: 100, 8: 200, 16: 400})
        self.assertEqual(calls, [])
        self.assertIn("fixed levels", result)

    def test_refinement_is_cut_by_the_deadline(self) -> None:
        tputs = {4: 100, 8: 200, 16: 400, 32: 390, 23: 450}
        calls, result = self.run_sweep(4 * self.STEP + 10, tputs)
        self.assertEqual(calls, [4, 8, 16, 32])
        self.assertEqual(result["output_tokens_per_s"], 400)
        calls, result = self.run_sweep(5 * self.STEP + 10, tputs)
        self.assertEqual(calls, [4, 8, 16, 32, 23])
        self.assertEqual((result["output_tokens_per_s"], result["peak_concurrency"]), (450, 23))


class DevSweep(unittest.TestCase):
    def run_main(
        self, tokens=None, *, fail_at=(), artifacts: bool = True, extra=(), vs_output: bool = False
    ):
        calls: list[tuple[int, float, float]] = []
        sleeps: list[float] = []
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cwd = root / "checkout"
            cwd.mkdir()
            saved = (
                replay.run_step,
                replay._check_trace,
                replay.time.sleep,
                sys.argv,
                os.getcwd(),
                os.environ.get("VIBESYS_ARTIFACTS_DIR"),
            )

            def fake_run_step(args, plan, temporary, concurrency, tokenizer, timeout):
                calls.append((concurrency, plan.warmup, plan.duration))
                if concurrency in fail_at:
                    raise ValueError("boom")
                return row(100.0 * concurrency, ttft=10.0 * concurrency), {
                    "requests_submitted": 7 * concurrency,
                    "ttft_censored_requests": 2,
                    "requests_cancelled_at_window_end": 5,
                    "server_prefix_hit_rate": 0.9,
                }

            replay.run_step, replay._check_trace = fake_run_step, lambda: None
            replay.time.sleep = sleeps.append
            sys.argv = [
                "replay.py",
                "--request-factory-engine",
                "x",
                "--sweep",
                "--test-tokenizer",
                "t",
                *extra,
            ]
            if vs_output:
                sys.argv += ["--vs-output", str(root / "vs.jsonl")]
            if tokens is not None:
                sys.argv += ["--", *tokens]
            os.environ["TRACELAB_SCALE_TESTING"] = "1"
            if artifacts:
                os.environ["VIBESYS_ARTIFACTS_DIR"] = str(root / "art")
            else:
                os.environ.pop("VIBESYS_ARTIFACTS_DIR", None)
            os.chdir(cwd)
            stdout, stderr = io.StringIO(), io.StringIO()
            try:
                with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                    status = replay.main()
            finally:
                os.chdir(saved[4])
                os.environ.pop("TRACELAB_SCALE_TESTING", None)
                if saved[5] is None:
                    os.environ.pop("VIBESYS_ARTIFACTS_DIR", None)
                else:
                    os.environ["VIBESYS_ARTIFACTS_DIR"] = saved[5]
                replay.run_step, replay._check_trace, replay.time.sleep, sys.argv = saved[:4]
            art = root / "art" / "sweep.json"
            data = json.loads(art.read_text()) if art.exists() else None
            written = (root / "vs.jsonl").exists()
        return status, calls, sleeps, stdout.getvalue(), stderr.getvalue(), data, written

    def test_defaults(self) -> None:
        status, calls, sleeps, stdout, _, data, written = self.run_main()
        self.assertEqual(status, 0)
        self.assertEqual(calls, [(4, 30, 60), (16, 30, 60), (64, 30, 60)])
        self.assertEqual(sleeps, [10, 10])
        self.assertFalse(written)
        self.assertEqual([s["concurrency"] for s in data["steps"]], [4, 16, 64])

    def test_args_set_levels_in_order_and_windows(self) -> None:
        tokens = [
            "--concurrency",
            "64,2,8",
            "--warmup-s",
            "5",
            "--window-s",
            "20",
            "--cooldown-s",
            "1",
        ]
        status, calls, sleeps, stdout, _, data, _ = self.run_main(tokens)
        self.assertEqual(status, 0)
        self.assertEqual(calls, [(64, 5, 20), (2, 5, 20), (8, 5, 20)])
        self.assertEqual(sleeps, [1, 1])
        self.assertEqual((data["warmup_s"], data["window_s"], data["cooldown_s"]), (5, 20, 1))

    def test_output_table_notice_and_artifact(self) -> None:
        _, _, _, stdout, _, data, _ = self.run_main(["--concurrency", "4,16"])
        self.assertIn("UNSCORED", stdout)
        self.assertIn("not comparable with `submit measure`", stdout)
        for word in ("conc", "tok/s", "ttft_p50", "tpot_p90", "requests", "ttft_censored", "cap"):
            self.assertIn(word, stdout)
        self.assertTrue(data["unscored"])
        step = data["steps"][1]
        self.assertEqual(step["concurrency"], 16)
        self.assertEqual(step["output_tokens_per_s"], 1600.0)
        self.assertEqual((step["requests"], step["ttft_censored_requests"]), (112, 2))
        self.assertTrue(step["latency_cap_met"])
        self.assertIsNone(step["error"])

    def test_no_artifact_without_the_variable(self) -> None:
        status, _, _, stdout, _, data, _ = self.run_main(artifacts=False)
        self.assertEqual((status, data), (0, None))
        self.assertIn("tok/s", stdout)

    def test_a_failed_step_is_reported_and_the_rest_run(self) -> None:
        status, calls, _, stdout, _, data, _ = self.run_main(
            ["--concurrency", "4,8,16"], fail_at={8}
        )
        self.assertEqual(status, 1)
        self.assertEqual([c[0] for c in calls], [4, 8, 16])
        self.assertEqual([s["error"] for s in data["steps"]], [None, "boom", None])
        self.assertIn("FAILED: boom", stdout)

    def test_cap_failure_is_shown_not_fatal(self) -> None:
        # ttft = 10 * c ms, so none fails; make a step miss the cap through the row helper.
        table = replay.format_sweep_table(
            [
                {
                    "concurrency": 4,
                    "error": None,
                    "output_tokens_per_s": 1.0,
                    "ttft_p50_ms": 1.0,
                    "ttft_p90_ms": 1.0,
                    "tpot_p50_ms": 1.0,
                    "tpot_p90_ms": 1.0,
                    "requests": 3,
                    "ttft_censored_requests": 0,
                    "latency_cap_met": False,
                }
            ]
        )
        self.assertIn("FAIL", table)

    def test_time_cap(self) -> None:
        tokens = ["--concurrency", "4,16,64,128", "--window-s", "300"]  # 4 * (30+300+10+90) > 1500
        status, calls, _, _, stderr, _, _ = self.run_main(tokens)
        self.assertEqual((status, calls), (1, []))
        self.assertIn("cap", stderr)
        self.assertIn("fewer concurrency levels or shorter", stderr)
        ok = ["--concurrency", "4,16,64,128", "--window-s", "200"]  # 4 * 330 = 1320
        self.assertEqual(self.run_main(ok)[0], 0)
        edge = {"concurrency": [4], "warmup_s": 0, "window_s": 1410, "cooldown_s": 0}
        self.assertEqual(replay.sweep_total_secs(edge), replay.SWEEP_MAX_TOTAL_SECS)

    def test_vs_output_is_optional_and_never_written(self) -> None:
        self.assertEqual(self.run_main()[0], 0)
        status, *_, written = self.run_main(vs_output=True)
        self.assertEqual(status, 0)
        self.assertFalse(written)

    def test_sweep_refuses_the_test_timing_flags(self) -> None:
        status, calls, *_ = self.run_main(extra=["--test-warmup-secs", "1"])
        self.assertEqual((status, calls), (1, []))

    def test_sweep_requires_vs_output(self) -> None:
        saved = sys.argv
        sys.argv = ["replay.py", "--request-factory-engine", "x"]
        try:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                replay.main()
        finally:
            sys.argv = saved

    def test_manifest_wiring(self) -> None:
        import tomllib

        manifest = tomllib.loads(
            (Path(__file__).resolve().parent.parent / "vibesys.input.toml").read_text()
        )
        bench = manifest["benchmark"]["command"]
        self.assertFalse([a for a in bench if a.startswith("--sweep") or a.startswith("--test")])
        self.assertNotIn("--", bench)
        # Development runs are `submit command` jobs in the task image, not a manifest section.
        self.assertNotIn("profile", manifest)


CANON = ["--concurrency", "8,24", "--warmup-s", "30", "--window-s", "90", "--cooldown-s", "10"]


class SweepArgs(unittest.TestCase):
    """Argument transport, `--check-args`, and the shared validation."""

    def check(self, tokens=None, *, extra=()):
        """Run main() as `--sweep --check-args [-- tokens]` in a fresh cwd."""
        with tempfile.TemporaryDirectory() as directory:
            cwd = Path(directory)
            saved = (sys.argv, os.getcwd())
            sys.argv = ["replay.py", "--sweep", "--check-args", *extra, "--", *(tokens or [])]
            os.chdir(cwd)
            out, err = io.StringIO(), io.StringIO()
            try:
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    try:
                        status = replay.main()
                    except SystemExit as exc:
                        status = exc.code
            finally:
                os.chdir(saved[1])
                sys.argv = saved[0]
        return status, out.getvalue(), err.getvalue()

    def settings(self, tokens):
        status, out, _ = self.check(tokens)
        self.assertEqual(status, 0, out)
        return json.loads(out)

    def test_spellings_canonicalise_identically(self) -> None:
        spellings = [
            ["--concurrency", "8,24", "--warmup-s", "30", "--window-s", "90", "--cooldown-s", "10"],
            ["--cooldown-s", "10", "--window-s=90", "--concurrency=8,24"],
            ["--window-s", "90", "--concurrency", "8,24"],
            ["--concurrency", "8,24", "--window-s", "090"],
        ]
        for tokens in spellings:
            with self.subTest(tokens=tokens):
                self.assertEqual(self.settings(tokens)["args"], CANON)

    def test_defaults_order_and_json_shape(self) -> None:
        data = self.settings([])
        self.assertEqual(
            data["args"],
            [
                "--concurrency",
                "4,16,64",
                "--warmup-s",
                "30",
                "--window-s",
                "60",
                "--cooldown-s",
                "10",
            ],
        )
        self.assertEqual(
            self.settings(["--concurrency", "64,2,8"])["settings"]["concurrency"], [64, 2, 8]
        )
        self.assertEqual(
            list(data),
            ["ok", "args", "settings"],
        )
        self.assertEqual(list(data["settings"]), list(replay.SWEEP_KEYS))
        status, out, err = self.check(["--concurrency", "8"])
        self.assertEqual((status, err, out.count("\n")), (0, "", 1))

    def test_rejections(self) -> None:
        bad = [
            ["--concurrency", "0"],
            ["--concurrency", "513"],
            ["--concurrency", "8,8"],
            ["--concurrency", "1,2,3,4,5,6,7,8,9"],
            ["--concurrency", "x"],
            ["--concurrency", ""],
            ["--concurrency", "8,,24"],
            ["--concurrency", "8.0"],
            ["--concurrency", "-4"],
            ["--concurrency", "1_0"],
            ["--concurrency", "+4"],
            ["--concurrency"],
            ["--warmup-s", "-1"],
            ["--window-s", "0"],
            ["--window-s", "1.5"],
            ["--cooldown-s", "x"],
            ["--window-s", "60", "--window-s", "70"],
            ["--bogus", "1"],
            ["8,24"],
            ["--request-factory-engine", "/bin/evil"],
            ["--vs-output", "/tmp/x"],
            ["--test-warmup-secs", "1"],
            ["--test-concurrencies", "4"],
            ["--url", "http://x"],
            ["--sweep"],
            ["--check-args"],
            ["--conc", "8"],
            ["--warmup", "1"],
            ["--window", "9"],
            ["--cool", "1"],
            ["--concurrency", "4,16,64,128", "--window-s", "300"],
        ]
        for tokens in bad:
            with self.subTest(tokens=tokens):
                status, out, err = self.check(tokens)
                self.assertEqual(status, 2)
                data = json.loads(out)
                self.assertFalse(data["ok"])
                self.assertEqual(set(data), {"ok", "error"})
                self.assertIn(data["error"], err)

    def test_time_cap_message(self) -> None:
        _, out, _ = self.check(["--concurrency", "4,16,64,128", "--window-s", "300"])
        self.assertIn("cap", json.loads(out)["error"])

    def test_task_flags_after_separator_never_reach_the_task_parser(self) -> None:
        # Before the separator the flags are the task's; after it they are unknown sweep args.
        status, out, _ = self.check(
            ["--request-factory-engine", "x"], extra=["--vs-output", "/nonexistent/o"]
        )
        self.assertEqual(status, 2)
        self.assertFalse(os.path.exists("/nonexistent/o"))

    def test_main_parser_has_no_abbreviations(self) -> None:
        saved = sys.argv
        sys.argv = ["replay.py", "--explor", "--check-arg", "--"]
        try:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                replay.main()
        finally:
            sys.argv = saved
        self.assertEqual(cm.exception.code, 2)

    def test_check_args_needs_sweep_and_no_task_flags(self) -> None:
        status, out, _ = self.check(["--concurrency", "8"])
        self.assertEqual(status, 0)  # no --request-factory-engine, no --vs-output
        saved = sys.argv
        sys.argv = ["replay.py", "--check-args"]
        try:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                replay.main()
        finally:
            sys.argv = saved

    def test_args_without_sweep_are_refused(self) -> None:
        saved = sys.argv
        sys.argv = [
            "replay.py",
            "--request-factory-engine",
            "x",
            "--vs-output",
            "/nonexistent/o",
            "--",
            "--concurrency",
            "8",
        ]
        try:
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                replay.main()
        finally:
            sys.argv = saved
        self.assertFalse(os.path.exists("/nonexistent/o"))

    def test_check_args_touches_nothing_outside(self) -> None:
        def boom(*a, **k):
            raise AssertionError("must not be called")

        names = ["run_step", "_tokenizer", "_check_trace", "write_corpus"]
        saved = {n: getattr(replay, n) for n in names}
        saved_sub = {n: getattr(replay.subprocess, n) for n in ("run", "Popen", "check_output")}
        import socket
        import urllib.request

        saved_net = (socket.socket.connect, urllib.request.urlopen)
        try:
            for n in names:
                setattr(replay, n, boom)
            for n in saved_sub:
                setattr(replay.subprocess, n, boom)
            socket.socket.connect, urllib.request.urlopen = boom, boom
            status, out, _ = self.check(["--concurrency", "8,24", "--window-s", "90"])
            bad = self.check(["--concurrency", "0"])
        finally:
            for n, f in saved.items():
                setattr(replay, n, f)
            for n, f in saved_sub.items():
                setattr(replay.subprocess, n, f)
            socket.socket.connect, urllib.request.urlopen = saved_net
        self.assertEqual((status, bad[0]), (0, 2))
        self.assertTrue(json.loads(out)["ok"])

    def test_run_path_echoes_canonical_args(self) -> None:
        run = DevSweep().run_main
        status, calls, sleeps, _, stderr, data, _ = run(
            tokens=[
                "--cooldown-s",
                "1",
                "--concurrency=24,8",
                "--warmup-s",
                "5",
                "--window-s",
                "20",
            ]
        )
        self.assertEqual(status, 0)
        self.assertEqual(calls, [(24, 5, 20), (8, 5, 20)])
        want = ["--concurrency", "24,8", "--warmup-s", "5", "--window-s", "20", "--cooldown-s", "1"]
        self.assertEqual(data["args"], want)
        self.assertIn(" ".join(want), stderr)
        # the defaults echo canonical args too
        _, _, _, _, _, data, _ = run(tokens=[])
        self.assertEqual(data["args"][:2], ["--concurrency", "4,16,64"])

    def test_run_path_rejects_bad_args_before_any_step(self) -> None:
        for tokens in (["--concurrency", "0"], ["--vs-output", "x"], ["--conc", "4"]):
            with self.subTest(tokens=tokens):
                status, calls, _, stdout, stderr, data, written = DevSweep().run_main(tokens=tokens)
                self.assertEqual((status, calls, data, written), (1, [], None, False))
                self.assertIn("sweep args", stderr)

    def test_stdlib_only_isolated_subprocess(self) -> None:
        import subprocess as sp

        script = str(Path(__file__).resolve().parent / "replay.py")
        with tempfile.TemporaryDirectory() as directory:
            proc = sp.run(
                [
                    sys.executable,
                    "-I",
                    script,
                    "--sweep",
                    "--check-args",
                    "--",
                    "--concurrency",
                    "8,24",
                    "--window-s",
                    "90",
                ],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout)["args"], CANON)
            proc = sp.run(
                [
                    sys.executable,
                    "-I",
                    script,
                    "--sweep",
                    "--check-args",
                    "--",
                    "--concurrency",
                    "0",
                ],
                cwd=directory,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 2)
            self.assertFalse(json.loads(proc.stdout)["ok"])
            self.assertTrue(proc.stderr.strip())


class Defaults(unittest.TestCase):
    NAMES = (
        "concurrencies",
        "warmup_secs",
        "duration_secs",
        "cooldown_secs",
        "deadline_secs",
        "pool_limit",
        "corpus_lines",
        "tokenizer",
        "model",
    )

    def test_sweep_parameters(self) -> None:
        self.assertEqual(replay.CONCURRENCIES, (4, 8, 16, 32, 64, 128, 256))
        self.assertEqual(replay.FIXED_LEVELS, (4,))
        self.assertEqual(
            (replay.WARMUP_SECS, replay.DURATION_SECS, replay.COOLDOWN_SECS), (60, 180, 15)
        )
        self.assertEqual((replay.TPOT_P50_CAP_MS, replay.TTFT_P50_CAP_MS), (100.0, 10_000.0))
        self.assertEqual((replay.SATURATION_GAIN, replay.PEAK_TOLERANCE), (1.03, 0.03))
        self.assertGreater(replay.TOKEN_POOL_LIMIT, 10 * 262144)

    def test_defaults_and_the_test_opt_in(self) -> None:
        none = argparse.Namespace(**{f"test_{n}": None for n in self.NAMES})
        plan = replay._test_plan(none)
        self.assertEqual(
            (plan.concurrencies, plan.warmup, plan.duration, plan.cooldown, plan.pool_limit),
            (replay.CONCURRENCIES, 60, 180, 15, replay.TOKEN_POOL_LIMIT),
        )
        tweaked = argparse.Namespace(**{**vars(none), "test_warmup_secs": 1.0})
        saved = os.environ.pop("TRACELAB_SCALE_TESTING", None)
        try:
            with self.assertRaises(ValueError):
                replay._test_plan(tweaked)
            os.environ["TRACELAB_SCALE_TESTING"] = "1"
            self.assertEqual(replay._test_plan(tweaked).warmup, 1.0)
            odd = argparse.Namespace(**{**vars(none), "test_concurrencies": "8,3"})
            with self.assertRaises(ValueError):
                replay._test_plan(odd)
        finally:
            os.environ.pop("TRACELAB_SCALE_TESTING", None)
            if saved is not None:
                os.environ["TRACELAB_SCALE_TESTING"] = saved

    def test_corpus_lines_are_unique_and_nonced_per_call(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            a, b = Path(directory) / "a", Path(directory) / "b"
            replay.write_corpus(a, 50)
            replay.write_corpus(b, 50)
            self.assertEqual(len(set(a.read_text().splitlines())), 50)
            self.assertFalse(set(a.read_text().splitlines()) & set(b.read_text().splitlines()))


if __name__ == "__main__":
    unittest.main()
