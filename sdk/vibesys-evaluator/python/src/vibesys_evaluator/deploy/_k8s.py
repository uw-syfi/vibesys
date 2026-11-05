"""Kubernetes implementation of the deploy interface (owned by the k8s deploy work package).

Stub: ``deployer`` returns the ``Deployer`` for an ``App`` and ``control``
runs ``bench.py ctl ...``. The orchestrator calls only these two.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

    from vibesys_evaluator._private.deploy import Deployer
    from vibesys_evaluator.deploy.k8s import App


def deployer(app: App[Any]) -> Deployer:
    """The deployer that brings ``app`` up in a fresh namespace.

    Args:
        app: The application.

    Raises:
        NotImplementedError: Until the k8s deploy layer lands.
    """
    msg = f"deploy.k8s is not implemented yet (app {app.name})"
    raise NotImplementedError(msg)


def control(app: App[Any], argv: Sequence[str]) -> int:
    """Run ``bench.py ctl ARGV`` (restart, flush, stop, start) against a running sample.

    Args:
        app: The application.
        argv: The arguments after ``ctl``.

    Raises:
        NotImplementedError: Until the k8s deploy layer lands.
    """
    msg = f"deploy.k8s ctl is not implemented yet (app {app.name}, {list(argv)})"
    raise NotImplementedError(msg)
