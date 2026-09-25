"""Shared fixtures for constructing the active run manifest contract."""

from vs_project.api import RunExecutionRecord


def run_execution_record() -> RunExecutionRecord:
    """Provide inert host settings for read-only persisted-run fixtures."""
    return RunExecutionRecord(
        model="test-model",
        agent_backend="stub",
        compute_backend="cpu",
        requested_profiler="none",
        resolved_profiler="none",
    )
