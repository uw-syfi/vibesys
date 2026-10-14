"""The dynamic loop is planned only for a run environment that opens candidate sandboxes."""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.vibesys.orchestration.dynamic.core_policy._inputs import (
    candidate_environment,
    plan_of,
    resolved,
)

from vibesys.errors import ConfigurationError
from vs_sim.api.testing import examples


@settings(max_examples=examples(pr=8, nightly=100))
@given(
    kind=st.sampled_from(["skypilot", "metal", "custom-env"]),
    cli_sandboxed=st.booleans(),
    share_agent_session=st.booleans(),
)
def test_an_environment_without_candidate_sandboxes_is_refused_naming_it_and_why(
    kind: str, *, cli_sandboxed: bool, share_agent_session: bool
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        run = resolved(Path(directory))
        environment = replace(
            run.environment,
            env_kind=kind,
            cli_sandboxed=cli_sandboxed,
            share_agent_session=share_agent_session,
        )
        run = replace(run, environment=environment)
        if environment.supports_parallel_candidate_evaluation:
            plan_of(run)
            return
        with pytest.raises(ConfigurationError, match="isolated candidate sandboxes") as raised:
            plan_of(run)

    assert f"'{kind}' run environment" in str(raised.value)
    assert " because " in str(raised.value)


def test_an_environment_with_candidate_sandboxes_is_planned(tmp_path: Path) -> None:
    plan_of(resolved(tmp_path, environment=candidate_environment()))
