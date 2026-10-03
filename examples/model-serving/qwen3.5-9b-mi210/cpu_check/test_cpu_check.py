"""Tests for the tiny-model CPU check. Run from the bundle root:

uv run --project cpu_check python -m pytest cpu_check
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from cpu_check import sessions

BUNDLE = Path(__file__).resolve().parents[1]
FAKE_SERVER = Path(__file__).parent / "testdata" / "caching_server.py"
FAKE_SCHEDULER = Path(__file__).parent / "testdata" / "scheduler_server.py"


class HashReference:
    """Deterministic stand-in for the reference engine: tokens derived from the prompt."""

    def greedy(self, prompt: list[int], n: int) -> list[int]:
        return [(sum(prompt) * 31 + i) % 500 + 1 for i in range(n)]

    def margins(self, prompt: list[int], cont: list[int]) -> list[float]:
        return [float(t % 3) for t in cont]


@settings(max_examples=50, deadline=None)
@given(
    seed=st.integers(min_value=0, max_value=2**32 - 1),
    concurrency=st.one_of(st.just(0), st.integers(min_value=1, max_value=12)),
)
def test_each_round_extends_the_last_and_outgrows_its_state(seed: int, concurrency: int) -> None:
    names = sessions.concurrent_sessions(concurrency) if concurrency else sessions.SESSIONS
    rounds = sessions.plan(HashReference(), vocab_size=512, seed=seed, names=names)
    assert {r.session for r in rounds} == set(names)
    if concurrency:
        long_first = next(r for r in rounds if r.session == sessions.LONG_SESSION)
        assert len(long_first.prompt) == sessions.SHARED_PREFIX + sessions.LONG_FIRST_INPUT
    by_session: dict[str, list[sessions.Round]] = {}
    for r in rounds:
        by_session.setdefault(r.session, []).append(r)
    first_prompts = [rs[0].prompt for rs in by_session.values()]
    assert all(
        p[: sessions.SHARED_PREFIX] == first_prompts[0][: sessions.SHARED_PREFIX]
        for p in first_prompts
    )
    assert first_prompts[0] != first_prompts[1]
    for rs in by_session.values():
        assert len(rs) >= 3
        for prev, cur in zip(rs, rs[1:], strict=False):
            # A resuming engine sees exactly its previous context, plus fresh input...
            assert (
                cur.prompt[: len(prev.prompt) + len(prev.expected)] == prev.prompt + prev.expected
            )
            # ... and needs more state than the request that created the cached state.
            assert len(cur.prompt) + len(cur.expected) > len(prev.prompt) + len(prev.expected)
            assert len(cur.expected) > len(prev.expected)


def _candidate_root(tmp_path: Path, server: Path | None) -> Path:
    root = tmp_path / "candidate"
    for part in ("reference", "accuracy_checker", "cpu_check"):
        shutil.copytree(BUNDLE / part, root / part, ignore=shutil.ignore_patterns(".venv"))
    if server is not None:
        (root / "engine").mkdir()
        (root / "engine" / "__init__.py").touch()
        shutil.copy(server, root / "engine" / "server.py")
    return root


def _run_check(
    root: Path, *args: str, bug: str = "none", scheduler_bug: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cpu_check", "--root", str(root), *args],
        cwd=BUNDLE,
        env={**os.environ, "FAKE_CACHE_BUG": bug, "FAKE_SCHEDULER_BUG": scheduler_bug},
        capture_output=True,
        text=True,
        check=False,
    )


def test_reference_server_passes(tmp_path: Path) -> None:
    result = _run_check(_candidate_root(tmp_path, None))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "starting reference.server" in result.stdout
    assert "no cache hits" in result.stdout


def test_a_correct_prefix_cache_passes_with_hits(tmp_path: Path) -> None:
    result = _run_check(_candidate_root(tmp_path, FAKE_SERVER), "--expect-cache-hits")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "no cache hits" not in result.stdout
    assert "[ok] preflight replay" in result.stdout


def test_a_cache_that_only_snapshots_after_the_output_fails_the_preflight_replay(
    tmp_path: Path,
) -> None:
    # Chained rounds extend a finished request, so this cache hits them; the benchmark's
    # preflight repeats a prompt that produced one token, so the same cache misses it.
    result = _run_check(
        _candidate_root(tmp_path, FAKE_SERVER), "--expect-cache-hits", bug="output_only"
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "cache-hit rounds 0/6" not in result.stdout
    assert "[FAIL] preflight replay: second response reported cached_tokens=0" in result.stdout
    assert "8192-token prompt twice" in result.stdout


def test_the_preflight_replay_runs_only_when_cache_hits_are_expected(tmp_path: Path) -> None:
    result = _run_check(_candidate_root(tmp_path, FAKE_SERVER), bug="output_only")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "preflight replay" not in result.stdout


def test_cached_state_keeping_its_creators_capacity_fails_on_the_chained_round(
    tmp_path: Path,
) -> None:
    result = _run_check(_candidate_root(tmp_path, FAKE_SERVER), bug="stale_capacity")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "[ok] session A round 1" in result.stdout
    assert "[FAIL] session A round 2" in result.stdout
    assert "first server exception: ValueError: sequence length" in result.stdout
    assert "exceeds state capacity" in result.stdout


def test_r9_double_length_advance_fails(tmp_path: Path) -> None:
    # r9's h1 candidate advanced `state.length` after the model already had, so its
    # first decode past half the request overflowed the state ("exceeds state capacity").
    result = _run_check(_candidate_root(tmp_path, FAKE_SERVER), bug="double_length")

    assert result.returncode == 1, result.stdout + result.stderr
    assert "[FAIL] session A round 1" in result.stdout
    assert "exceeds state capacity" in result.stdout


def test_a_server_that_cannot_start_names_why(tmp_path: Path) -> None:
    root = _candidate_root(tmp_path, FAKE_SERVER)
    result = _run_check(root, bug="no_such_bug")

    assert result.returncode == 2, result.stdout + result.stderr
    assert "server exited" in result.stdout
    assert "unknown FAKE_CACHE_BUG" in result.stdout


def test_reference_server_passes_concurrent_mode(tmp_path: Path) -> None:
    result = _run_check(_candidate_root(tmp_path, None), "--concurrency", "8")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "concurrent: 9 sessions (one with a 4491-token first prompt)" in result.stdout
    assert "[ok] teacher-forced scoring of 1372 tokens" in result.stdout


# r13: every failed accuracy run of the continuous-batching candidate followed a passing
# sequential check. Each fake below passes it too and fails in concurrent mode.
@pytest.mark.parametrize(
    ("scheduler_bug", "failure", "server_exception"),
    [
        (
            "score_remainder",
            "[FAIL] teacher-forced scoring of 1372 tokens",
            "expected index [512, 1] to be no larger than self [347, 512]",
        ),
        (
            "inference_mode",
            "[FAIL] session long round 1 (prompt 4491 tokens",
            "Inference tensors cannot be saved for backward",
        ),
        ("slot_leak", "request failed", "admission: no free decode slot"),
    ],
)
def test_a_batched_path_bug_fails_only_the_concurrent_mode(
    tmp_path: Path, scheduler_bug: str, failure: str, server_exception: str
) -> None:
    root = _candidate_root(tmp_path, FAKE_SCHEDULER)

    sequential = _run_check(root, scheduler_bug=scheduler_bug)
    concurrent = _run_check(root, "--concurrency", "8", scheduler_bug=scheduler_bug)

    assert sequential.returncode == 0, sequential.stdout + sequential.stderr
    assert concurrent.returncode == 1, concurrent.stdout + concurrent.stderr
    assert failure in concurrent.stdout
    assert server_exception in concurrent.stdout
