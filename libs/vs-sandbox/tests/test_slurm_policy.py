"""The operator's Slurm policy is rejected early, naming the offending key."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from vs_sandbox.api.slurm import (
    AgentGpuConfig,
    AgentGpuRequestError,
    SlurmPolicyError,
    agent_gpu_capability,
    load_slurm_operator_settings,
    load_slurm_policy,
)
from vs_sim.api.testing import examples
from vs_slurm.api import LedgerPolicy, load_slurm_config

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


_TRANSPORTS = {
    "local": 'kind = "local"',
    "ssh": 'kind = "ssh"\nhost = "login"',
    "connector": 'kind = "connector"\ncommand = ["connector"]',
}
_AGENT_GPU = 'partitions = ["main"]\nmax_gpus = 8\nmax_time_minutes = 120\n'
_FIELDS = frozenset(AgentGpuConfig.model_fields)


def _agent_gpu_file(tmp_path: Path, table: str | None, transport: str = "local") -> Path:
    path = tmp_path / "slurm.toml"
    path.write_text(
        '[slurm]\nname = "cluster"\nremote_workspace_root = "/shared/vibesys"\n'
        f"[slurm.transport]\n{_TRANSPORTS[transport]}\n[vibesys]\n"
        + ("" if table is None else f"[vibesys.agent_gpu]\n{table}"),
        encoding="utf-8",
    )
    return path


_unknown_keys = st.from_regex(r"[a-z][a-z_]{0,15}", fullmatch=True).filter(
    lambda key: key not in _FIELDS
)


@settings(max_examples=examples(pr=5, nightly=25), deadline=None)
@given(key=_unknown_keys)
def test_an_unknown_agent_gpu_key_is_rejected_naming_it(key: str) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = _agent_gpu_file(Path(directory), f"{_AGENT_GPU}{key} = 1\n")
        with pytest.raises(SlurmPolicyError, match=rf"vibesys\.agent_gpu\.{key}$"):
            load_slurm_policy(path)


def test_a_key_of_the_slurm_gpu_table_that_the_slurm_file_says_elsewhere_is_rejected(
    tmp_path: Path,
) -> None:
    """Gates are sized by sbatch arguments and Slurm is reached through the transport's shell."""
    for key in ("gate_gpus", "gate_time_minutes", "srun_command", "scancel_command"):
        path = _agent_gpu_file(tmp_path, f"{_AGENT_GPU}{key} = 1\n")
        with pytest.raises(SlurmPolicyError, match=rf"vibesys\.agent_gpu\.{key}$"):
            load_slurm_policy(path)


@pytest.mark.parametrize("field", ["max_gpus", "max_time_minutes", "default_time_minutes"])
@settings(max_examples=examples(pr=5, nightly=15), deadline=None)
@given(value=st.integers(max_value=0))
def test_a_non_positive_agent_gpu_limit_is_rejected_naming_the_key(field: str, value: int) -> None:
    table = _AGENT_GPU.replace(f"{field} =", f"#{field} =") if field in _AGENT_GPU else _AGENT_GPU
    with tempfile.TemporaryDirectory() as directory:
        path = _agent_gpu_file(Path(directory), f"{table}{field} = {value}\n".replace("##", "#"))
        with pytest.raises(SlurmPolicyError, match=rf"vibesys\.agent_gpu\.{field}$"):
            load_slurm_policy(path)


@settings(max_examples=examples(pr=5, nightly=25), deadline=None)
@given(maximum=st.integers(1, 64), default=st.integers(1, 600), limit=st.integers(1, 600))
def test_a_default_time_above_the_time_limit_is_rejected_and_otherwise_requests_obey_limits(
    maximum: int, default: int, limit: int
) -> None:
    table = (
        f'partitions = ["main"]\nmax_gpus = {maximum}\n'
        f"max_time_minutes = {limit}\ndefault_time_minutes = {default}\n"
    )
    with tempfile.TemporaryDirectory() as directory:
        path = _agent_gpu_file(Path(directory), table)
        if default > limit:
            with pytest.raises(SlurmPolicyError, match=r"vibesys\.agent_gpu$"):
                load_slurm_policy(path)
            return
        config = load_slurm_policy(path).agent_gpu
    assert config is not None
    assert config.request(None, None).time_minutes == default
    assert config.request(maximum, limit).gpus == maximum
    with pytest.raises(AgentGpuRequestError, match="--gpus"):
        config.request(maximum + 1, None)
    with pytest.raises(AgentGpuRequestError, match="--time"):
        config.request(None, limit + 1)


def test_agent_gpu_is_offered_with_the_local_transport_only(tmp_path: Path) -> None:
    for transport in _TRANSPORTS:
        path = _agent_gpu_file(tmp_path, _AGENT_GPU, transport)
        config, policy = load_slurm_config(path), load_slurm_policy(path)
        if transport == "local":
            assert agent_gpu_capability(config, policy) is policy.agent_gpu
        else:
            with pytest.raises(SlurmPolicyError, match=rf"vibesys\.agent_gpu.*{transport}"):
                agent_gpu_capability(config, policy)


def test_without_an_agent_gpu_table_no_transport_offers_the_capability(tmp_path: Path) -> None:
    for transport in _TRANSPORTS:
        path = _agent_gpu_file(tmp_path, None, transport)
        assert agent_gpu_capability(load_slurm_config(path), load_slurm_policy(path)) is None


def _ledger_file(
    tmp_path: Path, transport_timeout_seconds: int, ledger: dict[str, float] | None
) -> Path:
    """An operator file with this transport timeout and, unless None, this ``[vibesys.ledger]``."""
    table = (
        ""
        if ledger is None
        else "[vibesys.ledger]\n" + "".join(f"{key} = {value}\n" for key, value in ledger.items())
    )
    path = tmp_path / "slurm.toml"
    path.write_text(
        '[slurm]\nname = "cluster"\nremote_workspace_root = "/shared/vibesys"\n'
        f"transport_timeout_seconds = {transport_timeout_seconds}\n"
        f"[slurm.transport]\n{_TRANSPORTS['ssh']}\n[vibesys]\n{table}",
        encoding="utf-8",
    )
    return path


@given(
    transport_timeout_seconds=st.integers(min_value=1, max_value=400),
    claim_seconds=st.one_of(st.none(), st.floats(min_value=1.0, max_value=5000.0)),
)
@settings(max_examples=examples(pr=40, nightly=400), deadline=None)
def test_a_ledger_whose_withdrawal_window_exceeds_the_longest_mark_is_rejected_naming_the_key(
    transport_timeout_seconds: int, claim_seconds: float | None
) -> None:
    """A withdrawal's mark holds ``7 x transport_timeout_seconds + 60`` s.

    A reader that cannot decode a mark holds it for the longest window any
    build may write (900 s). A window longer than that would let a reader undo
    a mark whose writer is still about to cancel the job; the claim window,
    whatever it is, changes nothing.
    """
    window = 7 * transport_timeout_seconds + 60
    ledger = {} if claim_seconds is None else {"claim_seconds": claim_seconds}
    policy = LedgerPolicy() if claim_seconds is None else LedgerPolicy(claim_seconds=claim_seconds)
    with tempfile.TemporaryDirectory() as directory:
        path = _ledger_file(Path(directory), transport_timeout_seconds, ledger)
        if window <= 900.0:
            assert load_slurm_operator_settings(path).policy.ledger == policy
            return
        with pytest.raises(SlurmPolicyError, match=r"slurm\.transport_timeout_seconds"):
            load_slurm_operator_settings(path)


def test_without_a_ledger_any_transport_timeout_loads(tmp_path: Path) -> None:
    """The window bounds only matter to the ledger."""
    assert load_slurm_operator_settings(_ledger_file(tmp_path, 400, None)).policy.ledger is None


@pytest.mark.parametrize("claim_seconds", [60.0, 300.0, 829.0])
def test_a_claim_window_shorter_than_the_mark_window_loads(
    tmp_path: Path, claim_seconds: float
) -> None:
    """No rule needs a claim to outlast a withdrawal mark (830 s at the default timeout).

    A mark this build cannot read holds at least ``MAX_WITHDRAWAL_WINDOW_SECONDS``
    whatever the claim window, so the operator may set a short claim window.
    """
    path = _ledger_file(tmp_path, 110, {"claim_seconds": claim_seconds})

    assert load_slurm_operator_settings(path).policy.ledger == LedgerPolicy(
        claim_seconds=claim_seconds
    )
