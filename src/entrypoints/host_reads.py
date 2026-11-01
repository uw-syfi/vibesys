"""``vibesys host <read>``: answer one registered read with only the host facts it touches.

Every read is a new process, so what a read imports is paid by every read: a catalog read does
tens of milliseconds of work and the import of what it never uses would cost ten times that.
:class:`ReadHost` is the host's facts (``entrypoints.host_facts``), each built the first time a
read asks for it. ``describe-machine`` and ``preflight-run`` genuinely read the whole host (its
registry, machine probes and launcher), so they build the helper's host on use.

Public surface: ``ReadHost``, ``read_source`` and ``serve_read``.
"""

from __future__ import annotations

import os
from functools import cached_property
from typing import TYPE_CHECKING

from entrypoints.host_facts import HostFacts
from server import lazy_module

if TYPE_CHECKING:
    from entrypoints import host_helper
    from server import (
        host_api,
        host_helper_requests,
        host_machine_facts,
        host_operation_reads,
        usage_limits,
    )
    from vs_agent.api.usage_limits import LimitsReader
    from vs_sim import api as sim
    from vs_sim.api import HttpsGet, Platform
else:
    host_helper = lazy_module("entrypoints.host_helper")
    sim = lazy_module("vs_sim.api")
    host_helper_requests = lazy_module("server.host_helper_requests")
    host_operation_reads = lazy_module("server.host_operation_reads")
    usage_limits = lazy_module("server.usage_limits")


class ReadHost(HostFacts):
    """The host facts a registered read asks for, and the reads that need the whole host."""

    def describe(self) -> host_machine_facts.MachineDescription:
        """What this machine offers runs; reads the whole host."""
        return host_helper_requests.describe_machine(self._whole_host)

    def preflight(
        self, request: host_api.RunPreflightRequest
    ) -> host_api.RunPreflightReply | host_api.HelperError:
        """Whether the run these inputs describe can start; reads the whole host."""
        return host_helper_requests.preflight_facts(request, self._whole_host)

    @cached_property
    def _whole_host(self) -> host_helper_requests.HelperHost:
        """The helper's host with every collaborator; built only by a read that needs them all."""
        return host_helper.real_host(self._environ, self._uid, self._threads)


def _https() -> HttpsGet:
    """The client for the one fetch a read makes, the price list; built when a refresh asks."""
    return sim.UrllibHttpsGet()


def _limits() -> LimitsReader:
    """The reader of provider limits; the agent library loads when a limits read asks."""
    return usage_limits.reader_here()


def read_source(platform: Platform) -> host_operation_reads.HostReadSource:
    """The read adapter over this process's user, host and ``platform``."""
    host = ReadHost(
        os.environ,
        os.getuid(),
        clock=platform.clock,
        threads=platform.threads,
        processes=platform.processes,
    )
    return host_operation_reads.HostReadSource(
        host,
        None,
        describe=host.describe,
        preflight=host.preflight,
        outside=host_operation_reads.Outside(https=_https, limits=_limits),
    )


def serve_read(cli: str, payload: bytes, *, platform: Platform) -> bytes:
    """Answer registered read ``cli`` for this process's user; the reply is the envelope bytes."""
    return host_operation_reads.answer_registered_read(cli, payload, read_source(platform))
