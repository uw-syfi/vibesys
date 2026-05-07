import sys
from abc import abstractmethod
from typing import Any

from agentshim.executor import CallbackCommandStreamSink, CommandExecutor, CommandRequest, HostCommandExecutor
from agentshim.events import AgentEventHandler
from agentshim.utils import get_interactive_env
from loguru import logger

from .base import CodingAgent


class CLIGenerationSession:
    """Handles a single generation request lifecycle."""

    def __init__(
        self,
        binary_name: str,
        env: dict[str, str],
        log_prefix: str,
        cmd: list[str],
        logger: Any,
        cwd: str | None = None,
        timeout: int | None = None,
        silent: bool = False,
        event_handler: AgentEventHandler | None = None,
        executor: CommandExecutor | None = None,
    ):
        self.binary_name = binary_name
        self.env = env
        self.log_prefix = log_prefix
        self.cmd = cmd
        self.logger = logger
        self.cwd = cwd
        self.timeout = timeout
        self.silent = silent
        self.event_handler = event_handler
        self.executor = executor or HostCommandExecutor()

        # State initialization
        self.stdout_lines: list[str] = []
        self.stderr_lines: list[str] = []
        self._at_line_start = True

    def _log_raw(self, message: str) -> None:
        """Log a raw message directly to output if not silent."""
        if not self.silent:
            self.logger.opt(raw=True).info(message)

    def _process_stdout(self, line: str) -> None:
        """Process a line from stdout."""
        line_stripped = line.rstrip("\n")
        if not self.silent:
            self.logger.info(line_stripped)

        if self.event_handler and line_stripped:
            self.event_handler.on_thinking(line_stripped + "\n")

        self.stdout_lines.append(line)

    def _print_stream_content(self, content: str):
        """Print streaming content with prefix handling."""
        if not content:
            return

        lines = content.split("\n")

        for i, line in enumerate(lines):
            is_last = i == len(lines) - 1

            if is_last:
                if line:
                    if self._at_line_start:
                        self._log_raw(f"{self.log_prefix} ")
                        self._at_line_start = False
                    self._log_raw(line)
            else:
                if self._at_line_start:
                    self._log_raw(f"{self.log_prefix} ")
                self._log_raw(line)
                self._log_raw("\n")
                self._at_line_start = True

    def _process_stderr(self, line: str) -> None:
        """Process a line from stderr."""
        line_stripped = line.rstrip("\n")
        if not self.silent:
            self.logger.bind(stderr=True).info(f"[STDERR] {line_stripped}")
        self.stderr_lines.append(line)

    def run(self, prompt: str) -> str:
        """Execute the generation process."""
        if not self.silent:
            self.logger.info(f"Running command: {' '.join(self.cmd)}")
            self._log_raw("=" * 80 + "\n")
            sys.stdout.flush()

        on_run_start = getattr(self.event_handler, "on_run_start", None)
        if on_run_start is not None:
            on_run_start(self.cmd)

        result = self.executor.run(
            CommandRequest(
                argv=self.cmd,
                stdin=prompt,
                cwd=self.cwd,
                env=self.env,
                timeout=self.timeout,
            ),
            CallbackCommandStreamSink(
                on_stdout=self._process_stdout,
                on_stderr=self._process_stderr,
            ),
        )

        on_run_end = getattr(self.event_handler, "on_run_end", None)
        if on_run_end is not None:
            on_run_end(result.returncode)

        self._log_raw("=" * 80 + "\n")

        if result.returncode != 0:
            raise RuntimeError(
                f"{self.binary_name} exited with code {result.returncode}: "
                f"{result.stderr}"
            )

        return "".join(self.stdout_lines).strip()


class CLICodingAgent(CodingAgent):
    """Base class for CLI-based coding agents."""

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

    def _extract_session_id(self, session: "CLIGenerationSession") -> str | None:
        """Extract a session/conversation ID from the completed session.

        Subclasses override this to parse the provider's output format.
        Returns ``None`` if no session ID could be determined.
        """
        return None

    @property
    def _log_prefix(self) -> str:
        """Return the log prefix for this agent."""
        return f"[{self.__class__.__name__}]"

    def _create_session(
        self,
        cmd: list[str],
        cwd: str | None = None,
        timeout: int | None = None,
        silent: bool = False,
    ) -> CLIGenerationSession:
        """Create a session for a single generation request.

        Can be overridden by subclasses to return specialized sessions.
        """
        return CLIGenerationSession(
            binary_name=self.binary_name,
            env=self.env,
            log_prefix=self._log_prefix,
            cmd=cmd,
            logger=self.logger,
            cwd=cwd,
            timeout=timeout,
            silent=silent,
            event_handler=self.event_handler,
            executor=self.executor,
        )

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
