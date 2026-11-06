"""``Run``: what the task's ``check``, ``benchmark`` and ``profile`` functions receive."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from vibesys_evaluator._private.sample import Sample


class Run:
    """One sample, as the task's functions see it: its phases and its report.

    A phase is a named step of the sample with a directory of its own for its
    files, which are kept with the job. ``report`` writes the sample's result
    (benchmark mode only, once). Domains extend it with their phase primitives.
    """

    __slots__ = ("_sample",)
    _sample: Sample

    def phase(self, name: str) -> Path:
        """Start the phase ``name``: a new, empty directory for its files, kept with the job.

        Args:
            name: Unique in the sample; 1 to 63 of a-z, 0-9, ``.``, ``_`` and ``-``.

        Returns:
            The directory.
        """
        return self._sample.phase_dir(name)

    def report(self, values: Mapping[str, float]) -> None:
        """Report the sample's metrics (benchmark mode, once): every declared metric, finite.

        Args:
            values: Metric name to value.
        """
        self._sample.report(values)


def bind(run: Run, sample: Sample) -> None:
    """Attach ``sample`` to a ``Run`` (or a domain's subclass) made with ``__new__``."""
    run._sample = sample


def new_run(sample: Sample) -> Run:
    """The ``Run`` a task function receives."""
    run = Run.__new__(Run)
    bind(run, sample)
    return run
