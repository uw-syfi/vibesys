"""Rendering parsed operator settings back to TOML is lossless."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vs_sandbox.api.slurm import (
    AgentGpuConfig,
    SlurmExecutionPolicy,
    SlurmOperatorSettings,
    SlurmPolicyError,
    load_slurm_operator_settings,
    render_slurm_operator_toml,
)
from vs_slurm.api import (
    PORT_PLACEHOLDER,
    LedgerPolicy,
    SlurmConfig,
    SlurmConnectorTransport,
    SlurmLocalTransport,
    SlurmService,
    SlurmSshTransport,
)

if TYPE_CHECKING:
    from pathlib import Path

_word = st.text(
    alphabet=st.characters(codec="utf-8", exclude_categories=("Cs", "Cc")), min_size=1, max_size=8
)
_argv = st.lists(_word, min_size=1, max_size=3).map(tuple)
_transports = st.one_of(
    st.builds(SlurmLocalTransport, kind=st.just("local"), shell_command=_argv, rsync_command=_argv),
    st.builds(
        SlurmSshTransport,
        kind=st.just("ssh"),
        host=st.from_regex(r"[a-z][a-z0-9.-]{0,10}", fullmatch=True),
        ssh_command=_argv,
    ),
    st.builds(SlurmConnectorTransport, kind=st.just("connector"), command=_argv),
)
_configs = st.builds(
    SlurmConfig,
    name=st.from_regex(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,20}", fullmatch=True),
    remote_workspace_root=st.just("/shared/root"),
    transport=_transports,
    sbatch_arguments=st.lists(_word, max_size=3).map(tuple),
    poll_interval_seconds=st.floats(0.5, 30, allow_nan=False),
    job_timeout_seconds=st.integers(1, 10_000),
    evaluation_capacity=st.integers(1, 8),
)
_agent_gpus = st.builds(
    AgentGpuConfig,
    partitions=st.lists(_word, min_size=1, max_size=3).map(tuple),
    max_gpus=st.integers(1, 8),
    max_time_minutes=st.just(120),
    default_time_minutes=st.integers(1, 120),
    srun_arguments=st.lists(_word, max_size=2).map(tuple),
    read_only_paths=st.lists(
        st.from_regex(r"/[a-z]{1,6}(/[a-z]{1,6}){0,2}", fullmatch=True), max_size=3, unique=True
    ).map(tuple),
    windows_command=st.none() | _argv,
    max_concurrent_jobs=st.none() | st.integers(1, 16),
)
_services = st.builds(
    SlurmService,
    command=_argv.map(lambda argv: (*argv, PORT_PLACEHOLDER)),
    readiness_url=_word.map(lambda url: f"http://{url}:{PORT_PLACEHOLDER}"),
    startup_timeout_seconds=st.integers(1, 600),
)
_ledgers = st.builds(
    LedgerPolicy,
    lease_seconds=st.floats(1, 3600, allow_nan=False),
    grace_seconds=st.floats(0, 86_400, allow_nan=False),
    run_while_detached=st.booleans(),
    keep_jobs_on_stop=st.booleans(),
)
_policies = st.builds(
    SlurmExecutionPolicy,
    setup_script=st.none() | st.just("/opt/setup.sh"),
    service=st.none() | _services,
    agent_gpu=st.none() | _agent_gpus,
    ledger=st.none() | _ledgers,
)


@given(config=_configs, policy=_policies)
def test_rendered_settings_parse_back_to_themselves(
    tmp_path_factory: pytest.TempPathFactory, config: SlurmConfig, policy: SlurmExecutionPolicy
) -> None:
    """Whatever a transport, service, agent GPU or ledger table holds, the rendering keeps it."""
    if policy.agent_gpu is not None and not isinstance(config.transport, SlurmLocalTransport):
        policy = policy.model_copy(update={"agent_gpu": None})
    settings = SlurmOperatorSettings(config, policy)
    path: Path = tmp_path_factory.mktemp("render") / "slurm.toml"
    path.write_text(render_slurm_operator_toml(settings), encoding="utf-8")

    assert load_slurm_operator_settings(path) == settings


def test_the_ledger_table_is_opt_in_and_rejects_unknown_keys(tmp_path: Path) -> None:
    """``[vibesys.ledger]`` turns the ledger on with its defaults; a misspelt key is an error."""
    base = '[slurm]\nname = "c"\nremote_workspace_root = "/r"\n[slurm.transport]\nkind = "ssh"\nhost = "h"\n'
    path = tmp_path / "slurm.toml"
    path.write_text(base, encoding="utf-8")
    assert load_slurm_operator_settings(path).policy.ledger is None
    path.write_text(base + "[vibesys.ledger]\n", encoding="utf-8")
    assert load_slurm_operator_settings(path).policy.ledger == LedgerPolicy()
    path.write_text(base + "[vibesys.ledger]\ngrace_seconds = 60\n", encoding="utf-8")
    ledger = load_slurm_operator_settings(path).policy.ledger
    assert ledger is not None
    assert ledger.grace_seconds == 60
    assert ledger.run_while_detached
    assert not ledger.keep_jobs_on_stop
    path.write_text(base + "[vibesys.ledger]\ngrace_second = 60\n", encoding="utf-8")
    with pytest.raises(SlurmPolicyError, match=r"ledger\.grace_second"):
        load_slurm_operator_settings(path)
