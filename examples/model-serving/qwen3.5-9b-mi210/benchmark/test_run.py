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
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
