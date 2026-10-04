"""The operator's Slurm policy is rejected early when its port has no service."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vs_sandbox.api.slurm import SlurmPolicyError, load_slurm_policy

if TYPE_CHECKING:
    from pathlib import Path

_SERVICE = (
    "[vibesys.service]\n"
    'command = ["python", "-m", "server", "--port", "VIBESYS_DYNAMIC_PORT"]\n'
    'readiness_url = "http://127.0.0.1:VIBESYS_DYNAMIC_PORT/health"\n'
    "startup_timeout_seconds = 30\n"
)


def _write(tmp_path: Path, name: str, *, service: bool) -> Path:
    path = tmp_path / "slurm.toml"
    path.write_text(
        '[slurm]\nname = "cluster"\n[vibesys]\n'
        f'{name} = ["--base-url", "http://127.0.0.1:VIBESYS_DYNAMIC_PORT/v1"]\n'
        + (_SERVICE if service else ""),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("name", ["accuracy_arguments", "benchmark_arguments"])
def test_evaluator_arguments_with_the_port_need_a_service(tmp_path: Path, name: str) -> None:
    with pytest.raises(SlurmPolicyError, match=rf"invalid settings: vibesys\.{name}$"):
        load_slurm_policy(_write(tmp_path, name, service=False))

    policy = load_slurm_policy(_write(tmp_path, name, service=True))

    assert "http://127.0.0.1:VIBESYS_DYNAMIC_PORT/v1" in getattr(policy, name)
