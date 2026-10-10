"""The dynamic loop is planned only for a run environment that opens candidate sandboxes."""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic.core_policy._inputs import (
    COMMIT,
    bounds_of,
    options_of,
    resolved,
    run_request,
)

from vibesys.errors import ConfigurationError
from vibesys.orchestration.dynamic import PLUGIN
from vs_core.api import RevisionRef
from vs_runtime.api import CoreRunContext

if TYPE_CHECKING:
    from vs_runtime.api.infrastructure import RunEnvironmentView


def _plan(root: Path, environment: RunEnvironmentView) -> None:
    request = run_request(root)
    run = resolved(root)
    bounds = bounds_of(request)
    assert PLUGIN.core is not None
    PLUGIN.core.plan(
        CoreRunContext(
            run_id="policy",
            options=options_of(request),
            facts=run.facts,
            baseline=RevisionRef.of_git_commit(COMMIT),
            evaluation_plan=run.evaluation_plan,
            environment=environment,
            artifacts=run.artifacts,
            evaluation_capacity=1,
            queue_allowance_seconds=bounds.queue_allowance_seconds,
            observe_interval_seconds=bounds.observe_interval_seconds,
            observe_backoff_cap_seconds=bounds.observe_backoff_cap_seconds,
            max_run_seconds=bounds.max_run_seconds,
        )
    )


@given(
    kind=st.sampled_from(["skypilot", "metal", "custom-env"]),
    cli_sandboxed=st.booleans(),
    share_agent_session=st.booleans(),
)
def test_an_environment_without_candidate_sandboxes_is_refused_naming_it_and_why(
    kind: str, *, cli_sandboxed: bool, share_agent_session: bool
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        base = resolved(Path(directory)).environment
        environment = replace(
            base,
            env_kind=kind,
            cli_sandboxed=cli_sandboxed,
            share_agent_session=share_agent_session,
            parallel_candidate_blocker=None if cli_sandboxed and not share_agent_session else "x",
        )
        if environment.supports_parallel_candidate_evaluation:
            _plan(Path(directory), environment)
            return
        with pytest.raises(ConfigurationError, match="isolated candidate sandboxes") as raised:
            _plan(Path(directory), environment)

    assert f"'{kind}' run environment" in str(raised.value)
    assert str(environment.parallel_candidate_obstacle) in str(raised.value)


def test_an_environment_with_candidate_sandboxes_is_planned(tmp_path: Path) -> None:
    environment = replace(
        resolved(tmp_path).environment,
        env_kind="docker",
        cli_sandboxed=True,
        share_agent_session=False,
        parallel_candidate_blocker=None,
    )

    _plan(tmp_path, environment)
