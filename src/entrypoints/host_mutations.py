"""``vibesys host`` commands that change something.

They start or terminate a run, change a cluster, or create or copy a project.

Every command is a new process, so a command pays for every module its family imports. The
reads, the watches and the host calls never import this module: ``entrypoints.host`` loads it
when a mutation is dispatched, and then it needs all of what it imports.

Public surface: ``serve_mutation``.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from entrypoints.host_facts import HostFacts
from entrypoints.host_helper import real_host
from server import lazy_module
from server.host_api import DetachedLaunchFailure, HelperError
from server.host_compute import ComputeError, apply_registered_cluster
from server.host_examples import ProjectLookupError
from server.host_helper_requests import (
    apply_registered_project,
    launch_registered_run,
    registry_start_request,
)
from vibesys.api.host import terminate_host
from vibesys.api.host_operations.api import (
    ClusterRefusedError,
    PricesDamagedError,
    PricesRefusedError,
    PricesUnknownError,
    ProjectRefusedError,
    StartRefusedError,
    serve_cluster,
    serve_project,
    serve_start,
    serve_usage_prices,
)
from vs_project.api import Project
from vs_proto.api import host_payloads as pb

if TYPE_CHECKING:
    from server import usage_pricing, usage_scan
    from vs_sim.api import Platform
else:
    # A price save is the only mutation that reads the usage state; the others do not pay for it.
    usage_pricing = lazy_module("server.usage_pricing")
    usage_scan = lazy_module("server.usage_scan")

_CLUSTER_SLOTS = frozenset({"put_cluster", "remove_cluster", "test_cluster"})


def serve_mutation(slot: str, payload: bytes, platform: Platform) -> bytes:
    """Dispatch the mutation registered as ``slot``. Cluster and project changes are not starts."""
    if slot == "terminate_run":
        return terminate_host(payload, platform=platform)
    if slot == "start_run":
        return _serve_start(payload, platform)
    if slot in _CLUSTER_SLOTS:
        return _serve_cluster(payload, platform)
    if slot == "usage_prices_save":
        return _serve_usage_prices(payload)
    return _serve_project(payload, platform)


def _serve_usage_prices(payload: bytes) -> bytes:
    """Admit one price save, then apply it. Replay does not apply it again."""

    def apply(request: pb.UsagePricesSaveRequest) -> pb.UsagePricesSaveResponse:
        try:
            return usage_scan.save_prices(usage_scan.usage_directory(), request)
        except usage_pricing.PriceBookUnreadableError as exc:
            raise PricesDamagedError(str(exc)) from exc
        except usage_pricing.PriceBookError as exc:
            raise PricesRefusedError(str(exc)) from exc
        except OSError as exc:
            raise PricesUnknownError from exc

    return serve_usage_prices(
        payload, store=Project.admissions(), host_user=str(os.getuid()), apply=apply
    )


def _serve_project(payload: bytes, platform: Platform) -> bytes:
    """Admit one project change, then apply it. Replay does not apply it again."""
    host = real_host(os.environ, os.getuid(), platform.threads)

    def apply(slot: str, request: object) -> object:
        if not isinstance(request, pb.CreateExampleRequest | pb.CopyProjectRequest):
            message = "project request is not a project mutation"
            raise ProjectRefusedError(message)
        try:
            return apply_registered_project(slot, request, host)
        except (ProjectLookupError, TypeError, ValueError) as exc:
            raise ProjectRefusedError(str(exc)) from exc

    return serve_project(
        payload,
        store=Project.admissions(),
        host_user=str(os.getuid()),
        apply=apply,
    )


def _serve_cluster(payload: bytes, platform: Platform) -> bytes:
    """Admit one cluster change, then apply it. Replay does not apply it again."""
    facts = HostFacts(
        os.environ,
        os.getuid(),
        clock=platform.clock,
        threads=platform.threads,
        processes=platform.processes,
    )
    compute = facts.compute

    def apply(slot: str, request: object) -> object:
        if not isinstance(
            request, pb.PutClusterRequest | pb.RemoveClusterRequest | pb.TestClusterRequest
        ):
            message = "cluster request is not a cluster mutation"
            raise ClusterRefusedError(message)
        try:
            return apply_registered_cluster(slot, request, compute)
        except (ComputeError, TypeError, ValueError) as exc:
            raise ClusterRefusedError(str(exc)) from exc

    return serve_cluster(
        payload,
        store=Project.admissions(),
        host_user=str(os.getuid()),
        apply=apply,
    )


def _serve_start(payload: bytes, platform: Platform) -> bytes:
    """Admit the start, then launch through the helper. Replay does not launch."""

    def launch(request: pb.StartRunRequest) -> tuple[str, str]:
        try:
            body = registry_start_request(request)
        except (TypeError, ValueError) as exc:
            raise StartRefusedError(str(exc)) from exc
        host = real_host(os.environ, os.getuid(), platform.threads)
        try:
            reply = launch_registered_run(body, host)
        except ProjectLookupError as exc:
            raise StartRefusedError(str(exc)) from exc
        if isinstance(reply, HelperError):
            raise StartRefusedError(reply.message)
        started = reply.launch
        if isinstance(started, DetachedLaunchFailure):
            raise StartRefusedError(started.message)
        return started.run_id, started.id

    return serve_start(
        payload,
        store=Project.admissions(),
        launch=launch,
        host_user=str(os.getuid()),
    )
