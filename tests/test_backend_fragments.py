"""Contract tests for the backend prompt-fragment registry.

Every :class:`ComputeBackend` the agent can target must have a
``ComputeBackendFragment`` registered *and* the fragment ``.j2`` files on
disk. Otherwise the backend-aware ``Prompt`` path (used by the plain
issue-tracker loop) raises at construction for that backend — a compute
backend can be registered in ``backends/`` yet silently break here.
"""

from __future__ import annotations

import pytest

from vibe_serve.constants import ComputeBackend
from vibe_serve.prompts import (
    _FRAGMENT_IMPLS,
    _build_env,
    get_backend_fragment,
)


@pytest.mark.parametrize("backend", list(ComputeBackend), ids=lambda b: b.value)
def test_every_backend_has_registered_fragment(backend: ComputeBackend) -> None:
    assert backend in _FRAGMENT_IMPLS, (
        f"{backend!r} has a compute backend but no ComputeBackendFragment; "
        f"the backend-aware Prompt path (plain loop) would raise for it."
    )


@pytest.mark.parametrize("backend", list(ComputeBackend), ids=lambda b: b.value)
def test_every_backend_fragment_validates(backend: ComputeBackend) -> None:
    # validate() raises ValueError listing any missing <name>.j2 files.
    _FRAGMENT_IMPLS[backend].validate()


@pytest.mark.parametrize("backend", list(ComputeBackend), ids=lambda b: b.value)
def test_every_backend_fragment_renders(backend: ComputeBackend) -> None:
    frag = get_backend_fragment(backend, _build_env())
    rendered = frag.render_all()
    assert set(rendered) == set(type(frag).NAMES)
