"""The mark a process carries to say which step started it.

A step's work is found by its mark after the process that started it died: a stage command
starts with ``STEP_TAG_ENV=<tag>`` in its initial environment, which its descendants inherit and
the command cannot rewrite from outside. The mark is the process's environment, so it survives
the launching process with no record to keep.
"""

from __future__ import annotations

STEP_TAG_ENV = "VIBESYS_STEP_TAG"
"""The environment variable that names the step a process belongs to."""

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
