"""Test double implementations of CodingAgent for testing.

These test doubles provide controlled behavior for testing different
scenarios without requiring actual agent execution.
"""

import time
from typing import Any

from libs.agent_cli.base import CodingAgent

HEALTH_VERDICT_HEALTHY = (
    "<health_verdict>healthy</health_verdict>\n"
    "<health_assessment>All services healthy.</health_assessment>\n"
    "<diagnosis></diagnosis>\n"
    "<script_fixed>false</script_fixed>"
)


class StubAgent(CodingAgent):
    """Minimal agent that returns stub responses.

    Use this for tests that need an agent but don't care about its behavior.
    Supports optional model and response attributes used by various test
    scenarios (e.g. operator persistence, cleanup tests).

    When the prompt contains "assess" and "health" (i.e. health assessment),
    returns a well-formed health verdict XML so the deployer/monitor flow
    succeeds without extra mocking.
    """

    def __init__(self, response: str = "stub response", model=None):
        """Initialize the stub agent.

        Args:
            response: The canned response to return.
            model: Optional model identifier for tests that check it.
        """
        self.response = response
        self.model = model
        self.calls: list[dict[str, Any]] = []
        self.generate_calls: list[dict[str, Any]] = []

    def generate(self, prompt: str, cwd=None, timeout=300, silent=False, **kwargs) -> str:
        """Return a stub response and record the call.

        When the prompt looks like a health assessment request, returns a
        well-formed health verdict XML so the deployer/monitor flow succeeds
        without extra mocking.

        Args:
            prompt: The prompt (recorded but otherwise ignored).
            cwd: Optional working directory.
            timeout: Timeout value.
            silent: Whether output is suppressed.
            **kwargs: Additional arguments (ignored).

        Returns:
            str: The configured stub response.
        """
        call = {"prompt": prompt, "cwd": cwd, "timeout": timeout, "silent": silent}
        call.update(kwargs)
        self.calls.append(call)
        self.generate_calls.append(call)
        prompt_lower = prompt.lower()
        if "assess" in prompt_lower and "health" in prompt_lower:
            return HEALTH_VERDICT_HEALTHY
        return self.response

    def run(self, *args, **kwargs):
        """No-op run method for operator tests."""
        return {}

    def start_event_stream(self, *args, **kwargs):
        """No-op event stream for operator tests."""


class ErrorAgent(CodingAgent):
    """Agent that always raises errors.

    Use this to test error handling paths.
    """

    def __init__(self, error_message: str = "Simulated agent error"):
        """Initialize the error agent.

        Args:
            error_message: The error message to raise.
        """
        self.error_message = error_message

    def generate(self, prompt: str, **kwargs) -> str:  # type: ignore[override]
        """Raise a RuntimeError.

        Args:
            prompt: The prompt (ignored).
            **kwargs: Additional arguments (ignored).

        Raises:
            RuntimeError: Always raised with the configured error message.
        """
        raise RuntimeError(self.error_message)


class TimeoutAgent(CodingAgent):
    """Agent that simulates timeouts.

    Use this to test timeout handling.
    """

    def __init__(self, sleep_duration: float = 999999):
        """Initialize the timeout agent.

        Args:
            sleep_duration: How long to sleep before returning.
        """
        self.sleep_duration = sleep_duration

    def generate(self, prompt: str, timeout: int = 300, **kwargs) -> str:  # type: ignore[override]
        """Sleep longer than the timeout.

        Args:
            prompt: The prompt (ignored).
            timeout: The timeout value (used to sleep longer).
            **kwargs: Additional arguments (ignored).

        Returns:
            str: A response (but timeout should occur first).
        """
        time.sleep(self.sleep_duration)
        return "too late"


class TrackingAgent(CodingAgent):
    """Agent that tracks all calls for verification.

    Use this to verify agent interactions without relying on mocks.
    """

    def __init__(self, response: str = "tracking response"):
        """Initialize the tracking agent.

        Args:
            response: The response to return for all calls.
        """
        self.calls: list[dict[str, Any]] = []
        self.fix_request_count = 0
        self.generation_count = 0
        self.response = response

    def generate(self, prompt: str, **kwargs) -> str:  # type: ignore[override]
        """Track the call and return a response.

        Args:
            prompt: The prompt to track.
            **kwargs: Additional arguments to track.

        Returns:
            str: The configured response.
        """
        self.calls.append({"prompt": prompt, "kwargs": kwargs})
        self.generation_count += 1

        if "fix" in prompt.lower():
            self.fix_request_count += 1

        prompt_lower = prompt.lower()
        if "assess" in prompt_lower and "health" in prompt_lower:
            return HEALTH_VERDICT_HEALTHY
        return self.response

    def reset(self):
        """Reset all tracking state."""
        self.calls.clear()
        self.fix_request_count = 0
        self.generation_count = 0


class ConfigurableAgent(CodingAgent):
    """Agent with configurable responses for different scenarios.

    Use this for complex test scenarios that need different responses
    for different prompts.
    """

    def __init__(self):
        """Initialize the configurable agent."""
        self.responses: dict[str, str] = {}
        self.default_response = "default response"
        self.calls: list[dict[str, Any]] = []

    def set_response(self, keyword: str, response: str):
        """Configure a response for prompts containing a keyword.

        Args:
            keyword: Keyword to match in the prompt.
            response: Response to return when keyword is found.
        """
        self.responses[keyword.lower()] = response

    def set_default_response(self, response: str):
        """Set the default response for unmatched prompts.

        Args:
            response: The default response.
        """
        self.default_response = response

    def generate(self, prompt: str, **kwargs) -> str:  # type: ignore[override]
        """Return a configured response based on the prompt.

        Args:
            prompt: The prompt to analyze.
            **kwargs: Additional arguments.

        Returns:
            str: A response matching the prompt, or the default.
        """
        self.calls.append({"prompt": prompt, "kwargs": kwargs})

        # Check for matching keywords
        prompt_lower = prompt.lower()
        for keyword, response in self.responses.items():
            if keyword in prompt_lower:
                return response

        return self.default_response


class ScriptGeneratingAgent(CodingAgent):
    """Agent that simulates script generation.

    This agent creates actual script files when asked, useful for
    integration testing the deployment flow.
    """

    def __init__(
        self,
        generate_valid_scripts: bool = True,
        responses: list[str] | None = None,
    ):
        """Initialize the script generating agent.

        Args:
            generate_valid_scripts: If True, generate working scripts.
                                   If False, generate broken scripts.
            responses: Custom script contents (if None, uses defaults).
        """
        self.generate_valid_scripts = generate_valid_scripts
        self.calls: list[tuple[str, str | None, int]] = []
        self.responses = responses or [
            "#!/bin/bash\necho deploy",
            "#!/bin/bash\necho health",
        ]
        self.call_count = 0

    def generate(self, prompt: str, cwd: str | None = None, timeout: int = 300, **kwargs) -> str:  # type: ignore[override]
        """Generate scripts based on the prompt.

        Args:
            prompt: The prompt requesting script generation.
            cwd: Working directory for script creation.
            timeout: Timeout for generation.
            **kwargs: Additional arguments.

        Returns:
            str: A response indicating script generation.
        """
        self.calls.append((prompt, cwd, timeout))

        if cwd is None:
            return "No working directory specified"

        from pathlib import Path

        cwd_path = Path(cwd)
        sds_dir = cwd_path / ".sds"

        # Create .sds directory if needed
        sds_dir.mkdir(exist_ok=True)

        # Determine which file to write based on prompt content.
        # Both prompts mention both scripts (via the shared system prompt),
        # so we identify the *target* by finding which script path appears
        # last in the prompt -- the generation instruction is always at the
        # end, after the system-prompt preamble.
        filename = None
        prompt_lower = prompt.lower()
        last_deploy = prompt_lower.rfind("deploy.sh")
        last_health = prompt_lower.rfind("health_check.sh")
        if last_health > last_deploy:
            filename = "health_check.sh"
        elif last_deploy >= 0:
            filename = "deploy.sh"

        if filename:
            script_path = sds_dir / filename
            # Use custom response if provided, otherwise use defaults
            if self.responses and self.call_count < len(self.responses):
                content = self.responses[self.call_count % len(self.responses)]
            elif self.generate_valid_scripts:
                if filename == "deploy.sh":
                    content = "#!/bin/bash\necho 'Deployment successful'\nexit 0\n"
                else:
                    content = "#!/bin/bash\necho 'Health check passed'\nexit 0\n"
            else:
                if filename == "deploy.sh":
                    content = "#!/bin/bash\necho 'Deployment failed'\nexit 1\n"
                else:
                    content = "#!/bin/bash\necho 'Health check failed'\nexit 1\n"

            script_path.write_text(content, encoding="utf-8")
            script_path.chmod(0o755)

        self.call_count += 1
        return "I have generated the scripts."
