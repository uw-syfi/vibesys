"""Choose the Dev Container Features a run's editor image gets beyond the agent layer.

A run environment declares what its editor container needs: toolchains the task
requires and pip requirements its environment adds (the Modal environment adds
the ``modal`` client so a candidate's ``modal run`` works). This module turns
that declaration into Feature references for
:func:`vs_agent.api.images.agent_image`; it is pure, so the choice can be tested
without a Docker daemon. The Feature sources come from their owners: the pip
extras Feature lives next to the run environments, in ``features/``, and the
toolchains come from the catalog in :mod:`vs_sandbox.toolchain_features`.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from vs_agent.api.images import AGENT_USER
from vs_sandbox.api import LocalFeature, toolchain_features

if TYPE_CHECKING:
    from collections.abc import Collection

    from vs_sandbox.api import FeatureRef

#: ``devcontainer features test`` project folder: ``src/<id>/`` and ``test/<id>/``.
RUN_ENVIRONMENT_FEATURES_PROJECT = Path(__file__).resolve().parent / "features"


def image_features(
    toolchains: Collection[str] = (), pip_extras: Collection[str] = ()
) -> tuple[FeatureRef, ...]:
    """Return the Features for *toolchains* and *pip_extras*, in install order.

    Both selections are deduplicated and sorted, so their order never changes
    the result (and so the image cache key). The agent user joins the ``docker``
    group when the container runtime is selected.

    Raises:
        ValueError: A toolchain is unknown, or a pip requirement is empty or
            contains whitespace (requirements reach ``pip`` as separate words).
    """
    requirements = sorted(set(pip_extras))
    for requirement in requirements:
        if not requirement or requirement.split() != [requirement]:
            message = f"pip requirement must be one word without whitespace: {requirement!r}"
            raise ValueError(message)
    selected: list[FeatureRef] = []
    if requirements:
        selected.append(
            LocalFeature(
                RUN_ENVIRONMENT_FEATURES_PROJECT / "src" / "vibesys-pip-extras",
                options={"requirements": " ".join(requirements)},
            )
        )
    selected.extend(toolchain_features(toolchains, docker_group_users=(AGENT_USER,)))
    return tuple(selected)
