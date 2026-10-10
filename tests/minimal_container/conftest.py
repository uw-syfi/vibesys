"""Fixtures for the ``minimal_container`` tier: one editor container per default base image.

The tier is opt-in (``VIBESYS_MINIMAL_CONTAINER=1``) and needs Docker, no GPU. Run
it with ``scripts/run_minimal_container_tests.sh``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from tests.minimal_container.editor import BASES, Base, Editor, open_editor
from tests.slurm_cluster.cluster import docker_problem

if TYPE_CHECKING:
    from collections.abc import Iterator

ENABLE_ENV = "VIBESYS_MINIMAL_CONTAINER"
#: Limits the tier to some bases (comma-separated names), for a quick run.
BASES_ENV = "VIBESYS_MINIMAL_CONTAINER_BASES"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Skip the tier unless it is enabled; keep it on one xdist worker.

    The tier shares one editor container per base image, so its tests must not be
    spread over workers (each would build and start its own).
    """
    enabled = os.environ.get(ENABLE_ENV) == "1"
    problem = docker_problem() if enabled else None
    for item in items:
        if item.get_closest_marker("minimal_container") is None:
            continue
        item.add_marker(pytest.mark.xdist_group("minimal_container"))
        item.add_marker(pytest.mark.serial)
        if not enabled:
            item.add_marker(
                pytest.mark.skip(reason=f"set {ENABLE_ENV}=1 to run the minimal-container tier")
            )
        elif problem is not None:
            item.add_marker(pytest.mark.skip(reason=problem))


def _selected_bases(*, stand_ins: bool) -> tuple[Base, ...]:
    chosen = os.environ.get(BASES_ENV)
    fit = tuple(base for base in BASES if stand_ins or not base.stand_in)
    if not chosen:
        return fit
    names = {name.strip() for name in chosen.split(",")}
    known = {base.name for base in BASES}
    if names - known:
        message = (
            f"{BASES_ENV} names unknown bases {sorted(names - known)}; choose from {sorted(known)}"
        )
        raise pytest.UsageError(message)
    return tuple(base for base in fit if base.name in names)


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Run every test of the tier once per selected base image.

    A module that sets ``INCLUDE_STAND_INS = True`` also runs on the stand-in bases,
    which cover one property of a default image and are not fit for the rest.
    """
    if "editor" in metafunc.fixturenames:
        stand_ins = bool(getattr(metafunc.module, "INCLUDE_STAND_INS", False))
        metafunc.parametrize(
            "editor",
            _selected_bases(stand_ins=stand_ins),
            indirect=True,
            ids=lambda base: base.name,
        )


@pytest.fixture(scope="session")
def editor(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[Editor]:
    """The editor container built on the requested base image, shared by the session.

    A dummy credential lets the run request a CLI provider; no CLI makes a model call.
    """
    base: Base = request.param
    patch = pytest.MonkeyPatch()
    patch.setenv("ANTHROPIC_API_KEY", "sk-test-minimal-container")
    try:
        with open_editor(base, tmp_path_factory.mktemp(f"editor-{base.name}")) as opened:
            yield opened
    finally:
        patch.undo()


def expect_failure_for(
    request: pytest.FixtureRequest, editor: Editor, base_names: tuple[str, ...], issue: int
) -> None:
    """Mark the running test as a strict known failure on the named bases, tracked by *issue*.

    Strict: when the bug is fixed the test passes unexpectedly and fails the tier
    until this call is removed, so the marker cannot outlive the bug.
    """
    if editor.base.name in base_names:
        request.applymarker(
            pytest.mark.xfail(strict=True, reason=f"known bug: uw-syfi/vibesys#{issue}")
        )
