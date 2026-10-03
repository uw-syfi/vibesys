"""Tests for the tiny-model CPU check. Run from the bundle root:

uv run --project cpu_check python -m pytest cpu_check
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from cpu_check import sessions

BUNDLE = Path(__file__).resolve().parents[1]
FAKE_SERVER = Path(__file__).parent / "testdata" / "caching_server.py"


class HashReference:
    """Deterministic stand-in for the reference engine: tokens derived from the prompt."""

    def greedy(self, prompt: list[int], n: int) -> list[int]:
        return [(sum(prompt) * 31 + i) % 500 + 1 for i in range(n)]

    def margins(self, prompt: list[int], cont: list[int]) -> list[float]:
        return [float(t % 3) for t in cont]


@settings(max_examples=50, deadline=None)
@given(seed=st.integers(min_value=0, max_value=2**32 - 1))
def test_each_round_extends_the_last_and_outgrows_its_state(seed: int) -> None:
    rounds = sessions.plan(HashReference(), vocab_size=512, seed=seed)
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


def _run_check(root: Path, *args: str, bug: str = "none") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cpu_check", "--root", str(root), *args],
        cwd=BUNDLE,
        env={**os.environ, "FAKE_CACHE_BUG": bug},
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
