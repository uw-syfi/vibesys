"""Private product implementation of ``RunReader``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vibesys.api._session import InProcessAuxiliaryAgents, run_ready
from vibesys.composition import resolve_agent_specs
from vibesys.run.integration import LocalRunIntegration
from vibesys.run.profilers import validate_run_request
from vibesys.run.reader import open_reader_resources

if TYPE_CHECKING:
    from vibesys.api.assembly import SessionImplementations
    from vibesys.api.auxiliary import AuxiliaryAgents, RunReady
    from vibesys.plugin_catalog import OrchestrationRegistry
    from vibesys.run.contracts import RunRequest


class _LocalRunReader:
    """A finished run's reader: its resources are assembled once, and nothing of the run runs."""

    def __init__(
        self,
        request: RunRequest,
        *,
        registry: OrchestrationRegistry,
        implementations: SessionImplementations,
    ) -> None:
        validate_run_request(request, git=implementations.git)
        plugin = registry.resolve(request.orchestration.id).plugin
        agent_specs = resolve_agent_specs(
            request.config,
            plugin.agents,
            backend=request.agent_backend,
            provider=request.cli_provider,
            allow_unvetted_model=request.allow_unvetted_model,
        )
        self._integration = LocalRunIntegration(
            implementations.git_repository,
            implementations.state_stores,
            clock=implementations.timing.clock,
            git=implementations.git,
            github=implementations.github,
            threads=implementations.timing.threads,
            system=implementations.system,
        )
        self._implementations = implementations
        self._resources = open_reader_resources(
            request,
            self._integration,
            agent_specs=agent_specs,
            backend_factory=implementations.backend_factory,
        )
        self._ready = run_ready(self._resources, registry, implementations)

    def ready(self) -> RunReady:
        """The run's readiness facts, read from its saved record."""
        return self._ready

    def open_auxiliary_agents(self) -> AuxiliaryAgents:
        """A scope that owns the conversations opened in it."""
        return InProcessAuxiliaryAgents(
            self._resources,
            self._implementations.agents,
            self._integration.agent_events,
            self._implementations.timing.threads,
        )

    def close(self) -> None:
        """Release what the reader's integration holds."""
        self._integration.close()
