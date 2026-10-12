"""The vs-dst pytest plugin: one setting for how many crash points a suite tries.

Registered by the repository's root ``conftest.py`` (``pytest_plugins``), like the vs-sim
plugin, and outside ``src`` because it imports pytest.

``--dst-crash-points=sampled`` (the default) tries `SAMPLE_SIZE` evenly spaced boundaries per
crash kind, always including the first and the last. ``--dst-crash-points=all`` tries every one,
which is what the nightly job runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_dst.api import EVERY_POINT, SAMPLED, set_crash_point_policy

if TYPE_CHECKING:
    import pytest

OPTION = "--dst-crash-points"
POLICIES = {"sampled": SAMPLED, "all": EVERY_POINT}


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the option."""
    parser.getgroup("vs-dst", "deterministic lifecycle simulation").addoption(
        OPTION,
        choices=sorted(POLICIES),
        default="sampled",
        help="crash points a simulated run offers: a sample of its commits, or all of them",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Make the chosen policy the library's setting for this process."""
    set_crash_point_policy(POLICIES[config.getoption("dst_crash_points")])


def pytest_report_header(config: pytest.Config) -> str:
    """Say which policy is active."""
    return f"vs-dst: crash points {config.getoption('dst_crash_points')}"
