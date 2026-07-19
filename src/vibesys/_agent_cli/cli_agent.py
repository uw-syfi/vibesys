from abc import abstractmethod
from typing import Generic, TypeVar

# Re-exported so callers (and compat tests) can keep importing
# ``CLIGenerationSession`` from this module. agentshim's class is the single
# session implementation; provider modules subclass it per CLI.
from agentshim.cli_agent import CLIGenerationSession
from agentshim.events import AgentEventHandler
from agentshim.executor import CommandExecutor, HostCommandExecutor
from agentshim.utils import get_interactive_env
from loguru import logger

from .base import CodingAgent
from .hostsandbox import WorkspaceSandbox

__all__ = ["CLICodingAgent", "CLIGenerationSession"]

SessionT = TypeVar("SessionT", bound=CLIGenerationSession)


class CLICodingAgent(CodingAgent, Generic[SessionT]):
    """Base class for CLI-based coding agents.

    Generic over the provider's :class:`~agentshim.cli_agent.CLIGenerationSession`
    subclass, so ``_create_session`` / ``_extract_session_id`` overrides stay
    type-safe per provider.
    """

    def __init__(
        self,
        binary_name: str,
        model: str | None = None,
        event_handler: AgentEventHandler | None = None,
        *,
        executor: CommandExecutor | None = None,
    ):
        """Initialize the CLI coding agent.

        Args:
            binary_name: The name of the executable to use.
            model: Optional model name to use.
            event_handler: Optional event handler for UI updates.
            executor: Controls binary lookup, validation, and command execution.

        Raises:
            RuntimeError: If binary is not found in PATH or is not working.
        """
        self.executor = executor or HostCommandExecutor()
        self.env = get_interactive_env()
        self.binary_name = binary_name
        self.model = model
        self.event_handler = event_handler
        self.binary_path = self.executor.find_binary(binary_name, self.env)
        self.executor.check_binary(self.binary_path, self.env, timeout=10)
        self.logger = logger.bind(agent_prefix=self._log_prefix)
        self.session_id: str | None = None
        self._last_session: SessionT | None = None
        # Host-path filesystem confinement. Left ``None`` here (unconfined,
        # legacy behavior); the CLI runner installs a platform-specific
        # :class:`WorkspaceSandbox` on the host execution path. Container executors
        # leave it ``None`` because they are already externally sandboxed.
        self.sandbox: WorkspaceSandbox | None = None

    @abstractmethod
    def _get_command(self, prompt: str) -> list[str]:
        """Construct the command line arguments for a fresh session."""

    def _get_resume_command(self, prompt: str, session_id: str) -> list[str] | None:
        """Construct the command to resume a previous session.

        Returns ``None`` if the provider does not support session resumption,
        in which case :meth:`generate` falls back to :meth:`_get_command`.
        Subclasses override this to wire up provider-specific resume flags.
        """
        return None

    def _extract_session_id(self, session: SessionT) -> str | None:
        """Extract a session/conversation ID from the completed session.

        Subclasses override this to parse the provider's output format.
        Returns ``None`` if no session ID could be determined.
        """
        return None

    @property
    def _log_prefix(self) -> str:
        """Return the log prefix for this agent."""
        return f"[{self.__class__.__name__}]"

    @abstractmethod
    def _create_session(
        self,
        cmd: list[str],
        cwd: str | None = None,
        timeout: int | None = None,
        silent: bool = False,
    ) -> SessionT:
        """Create the provider-specific session for one generation request."""

    def generate(
        self,
        prompt: str,
        cwd: str | None = None,
        timeout: int | None = None,
        silent: bool = False,
    ) -> str:
        """Generate text using the CLI tool.

        Args:
            prompt: The prompt to send.
            cwd: Optional working directory.
            timeout: Timeout in seconds. ``None`` (default) means no timeout.
            silent: If True, suppress stdout printing of the agent's output.

        Returns:
            Generated text.
        """
        # Try to resume an existing session if we have one
        cmd = None
        if self.session_id is not None:
            cmd = self._get_resume_command(prompt, self.session_id)
        if cmd is None:
            cmd = self._get_command(prompt)

        # Confine the agent to its workspace at the OS level. Only applies on the
        # host path (a real ``cwd`` plus an installed sandbox policy); container
        # executors pass ``cwd=None`` and leave ``self.sandbox`` unset because
        # they are already externally sandboxed. bwrap re-establishes the working
        # directory inside the namespace via ``--chdir``.
        if self.sandbox is not None and cwd is not None:
            cmd = self.sandbox.wrap(cmd)

        session = self._create_session(cmd, cwd, timeout, silent)
        # Expose the session to callers (e.g. ``CliAgentRunner.invoke``
        # reads ``self._last_session.final_usage`` after this method
        # returns). Set it before ``run()`` so partial state is still
        # introspectable if ``run()`` raises mid-stream.
        self._last_session = session
        result = session.run(prompt)

        # Capture session ID for future continuation
        extracted_id = self._extract_session_id(session)
        if extracted_id is not None:
            self.session_id = extracted_id

        return result
