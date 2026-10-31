"""Typed host-operation facade: the admitted workflows (start, login, project, cluster, price save).

A process that serves one registered read imports the module of that read alone: ``reads`` for the
registered reads, ``saved_runs`` for the run-listing stream, ``candidate`` for candidate
inspection. This module brings the admission, login and start machinery, so none of them does.
The generated registry owns routing, validation, budgets and correlation.
"""

from vibesys.api.host_operations._compute import (
    ClusterRefusedError,
    ClusterUnknownError,
    serve_cluster,
)
from vibesys.api.host_operations._login import LoginRefusedFact, LoginStartedFact, serve_login
from vibesys.api.host_operations._project import (
    ProjectRefusedError,
    ProjectUnknownError,
    serve_project,
)
from vibesys.api.host_operations._start import StartRefusedError, StartUnknownError, serve_start
from vibesys.api.host_operations._usage_prices import (
    PricesDamagedError,
    PricesRefusedError,
    PricesUnknownError,
    serve_usage_prices,
)

__all__ = [
    "ClusterRefusedError",
    "ClusterUnknownError",
    "LoginRefusedFact",
    "LoginStartedFact",
    "PricesDamagedError",
    "PricesRefusedError",
    "PricesUnknownError",
    "ProjectRefusedError",
    "ProjectUnknownError",
    "StartRefusedError",
    "StartUnknownError",
    "serve_cluster",
    "serve_login",
    "serve_project",
    "serve_start",
    "serve_usage_prices",
]
