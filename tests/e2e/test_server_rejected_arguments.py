"""Rejected server launches release process-local ephemeral socket resources.

Every rejected argument list runs through the public ``main`` in a forked child of the test
(``run_in_child``: the child points the temp root at a private directory, which is
process-wide state). The real server process, and so the entrypoint's wiring, is the one of
``test_foreground_run_sigterm.py``.
"""

from __future__ import annotations

import tempfile
from functools import partial
from pathlib import Path
from string import ascii_lowercase

from hypothesis import example, given, settings
from hypothesis.strategies import SearchStrategy, integers, just, one_of, text

from entrypoints.server import main
from vs_sim.api.testing import examples, run_in_child

_LOWER = text(alphabet=ascii_lowercase, min_size=1, max_size=8)
_REJECTED_WEB_FLAGS: SearchStrategy[list[str]] = one_of(
    one_of(
        integers(min_value=65_536, max_value=1 << 31),
        integers(max_value=-1),
        _LOWER,
    ).map(lambda port: ["--web-port", str(port)]),
    # An origin is scheme, host and port: a path, or no scheme at all, is rejected.
    _LOWER.map(lambda path: ["--web-origin", f"http://localhost:5173/{path}"]),
    _LOWER.map(lambda host: ["--web-origin", host]),
    just(["--web-origin"]),
)


def _rejected_launch(arguments: list[str], *, web: bool) -> tuple[object, list[str]]:
    """Call ``main`` and list its temp root while the failure is still live.

    Call through ``run_in_child``: it redirects the process-wide temp root.
    """
    with tempfile.TemporaryDirectory() as base:
        temp_root = Path(base) / "temp"
        temp_root.mkdir()
        tempfile.tempdir = str(temp_root)
        invocation = (
            ["--web", "--web-instance", str(Path(base) / "instance.json"), *arguments]
            if web
            else arguments
        )
        try:
            main(invocation)
        except SystemExit as error:
            code = error.code
        else:
            message = "server entrypoint accepted rejected arguments"
            raise AssertionError(message)
        return code, sorted(path.name for path in temp_root.iterdir())


@example(arguments=["--web-port", "99999"])
@example(arguments=["--web-origin", "http://localhost:5173/app"])
@example(arguments=["--web-origin"])
@settings(max_examples=examples(pr=6, nightly=40), deadline=None)
@given(arguments=_REJECTED_WEB_FLAGS)
def test_a_rejected_web_flag_creates_no_ephemeral_socket_directory(
    arguments: list[str],
) -> None:
    assert run_in_child(partial(_rejected_launch, arguments, web=True)) == (2, [])


def test_a_missing_control_socket_creates_no_ephemeral_web_socket_directory() -> None:
    assert run_in_child(partial(_rejected_launch, [], web=False)) == (2, [])
