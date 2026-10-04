"""Runtime implementations imported only by application assembly code."""

from vs_runtime._runs import InProcessRuns, TaskRunHandle

__all__ = ["InProcessRuns", "TaskRunHandle"]
