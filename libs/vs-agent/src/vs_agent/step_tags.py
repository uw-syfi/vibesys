"""The marker an agent process carries to say which step started it.

A step's tag is its request id, the same in every epoch of a run. An agent process starts
with ``STEP_TAG_ENV=<tag>`` in its environment, so the process an earlier attempt of the step
left running is found by the tag alone (``vs_agent.orphans.reap_step_agents``). The marker is
the process's initial environment, which the agent cannot rewrite afterwards.
"""

from __future__ import annotations

STEP_TAG_ENV = "VIBESYS_STEP_TAG"
"""The environment variable that names the step an agent process belongs to."""

_FORBIDDEN = ("\n", "\0")


def step_tag_env(tag: str) -> dict[str, str]:
    """The environment that marks a process as started by the step *tag*.

    A tag that is empty or holds a newline or NUL cannot be one line of ``/proc/<pid>/environ``
    that a sweep can match exactly, so it is refused here rather than marking nothing.
    """
    if not tag or any(char in tag for char in _FORBIDDEN):
        message = f"step tag {tag!r} must be non-empty and hold no newline or NUL"
        raise ValueError(message)
    return {STEP_TAG_ENV: tag}
