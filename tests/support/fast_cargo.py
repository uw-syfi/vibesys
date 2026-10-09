"""Cargo environment for building test artifacts quickly.

Evaluator tests only need a correct runner or candidate, not a fast one: the
benchmarks they run last milliseconds and assert on result shape. The release
profiles of the example crates use thin LTO and one codegen unit, which makes
every cold build several times slower than an unoptimized one. Overriding the
profile through the environment keeps the ``release`` output directory (and the
crates' ``panic = "abort"`` setting) while skipping the optimization work.
"""

from __future__ import annotations

import os

_UNOPTIMIZED_RELEASE_PROFILE = {
    "CARGO_PROFILE_RELEASE_OPT_LEVEL": "0",
    "CARGO_PROFILE_RELEASE_LTO": "off",
    "CARGO_PROFILE_RELEASE_CODEGEN_UNITS": "256",
}


def fast_cargo_env(**extra: str) -> dict[str, str]:
    """Return the process environment with the release profile unoptimized."""
    return {**os.environ, **_UNOPTIMIZED_RELEASE_PROFILE, **extra}
