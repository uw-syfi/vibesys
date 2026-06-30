"""Snapshot resilience: a workspace file the snapshotting user cannot read
(e.g. a root-written mode-600 profiler artifact on the Docker path) must not
abort `git add -A` / the whole run. Such files are excluded, not fatal.
"""

from __future__ import annotations

import os
import subprocess
from types import SimpleNamespace

import pytest

from vibe_serve.context import _RunContext


def _git(ws, *args):
    subprocess.run(["git", *args], cwd=ws, check=True, capture_output=True)


def _make_ctx(ws):
    """A minimal stand-in exposing just the git-snapshot helpers."""
    ctx = SimpleNamespace(
        workspace=ws,
        lprint=lambda *a, **k: None,
        _GIT_ENV={
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        },
    )
    ctx._collect_unreadable = lambda: _RunContext._collect_unreadable(ctx)
    ctx._exclude_paths = lambda p: _RunContext._exclude_paths(ctx, p)
    ctx._git_run = lambda cmd, check=True: _RunContext._git_run(ctx, cmd, check=check)
    ctx._unreadable_from_stderr = _RunContext._unreadable_from_stderr
    return ctx


def test_workspace_gitignore_excludes_compiled_artifacts():
    ctx = SimpleNamespace(
        EXCLUDED_WORKSPACE_DIRS={".git", "__pycache__", "_mounts"},
        _ARTIFACT_GITIGNORE_PATTERNS=_RunContext._ARTIFACT_GITIGNORE_PATTERNS,
    )
    gi = _RunContext._workspace_gitignore(ctx)
    # accelerator artifacts an agent might drop into the workspace
    for pat in ("*.neff", "*.ntff", "neuron-compile-cache/"):
        assert pat in gi
    # still excludes the standard dirs
    assert ".git" in gi and "_mounts" in gi


def test_unreadable_from_stderr_parses_git_output():
    stderr = (
        'error: open("system_profile.json"): Permission denied\n'
        "error: unable to index file 'sub/ntrace.pb'\n"
        "fatal: adding files failed\n"
    )
    assert _RunContext._unreadable_from_stderr(stderr) == [
        "system_profile.json",
        "sub/ntrace.pb",
    ]


def test_collect_unreadable_finds_mode000_file(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "ok.txt").write_text("hi")
    secret = ws / "secret.bin"
    secret.write_text("x")
    os.chmod(secret, 0o000)
    try:
        ctx = _make_ctx(ws)
        assert ctx._collect_unreadable() == ["secret.bin"]
    finally:
        os.chmod(secret, 0o644)  # let pytest clean up


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read mode-000 files")
def test_git_add_all_excludes_unreadable_and_succeeds(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init")
    (ws / "code.py").write_text("print('hi')\n")
    secret = ws / "system_profile.json"
    secret.write_text("{}")
    os.chmod(secret, 0o600)  # owner-read, but pytest runs as the owner...
    os.chmod(secret, 0o000)  # ...so make it truly unreadable

    ctx = _make_ctx(ws)
    try:
        # Plain `git add -A` would exit 128 here; the resilient path must not.
        ctx._git_add_all = lambda: _RunContext._git_add_all(ctx)
        ctx._git_add_all()
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=ws, capture_output=True, text=True, check=True,
        ).stdout.split()
        assert "code.py" in staged
        assert "system_profile.json" not in staged
        # The offender is recorded in the local-only exclude file.
        exclude = (ws / ".git" / "info" / "exclude").read_text()
        assert "system_profile.json" in exclude
    finally:
        os.chmod(secret, 0o644)
