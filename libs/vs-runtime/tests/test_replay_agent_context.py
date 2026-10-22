"""Agent identity follows typed fact inputs while retaining rendered prompt policy."""

from copy import deepcopy

from hypothesis import given
from hypothesis import strategies as st
from protobuf import Oneof

from vs_core.api.replay import classify_context
from vs_proto.api import core_common
from vs_proto.api import replay_agent as pb

from .test_replay_sessions import SCOPE, SESSION


@given(value=st.text(alphabet="abcdefghijklmnopqrstuvwxyz0123456789", min_size=1, max_size=30))
def test_agent_policy_drift_reuses_facts_and_changed_facts_change_identity(value: str) -> None:
    request = pb.ReplayAgentRequest(
        scope=SCOPE,
        within_seconds=30,
        action=Oneof(
            "turn",
            pb.ReplayAgentTurn(
                session=deepcopy(SESSION),
                invocation="turn-1",
                workspace=SCOPE,
                max_turns=1,
                prompts=[
                    core_common.ArtifactRef(
                        artifact_id=core_common.ArtifactId(root="prompt", kind="artifact"),
                        digest="old-prompt",
                    )
                ],
                artifact_dependencies=[
                    core_common.ArtifactRef(
                        artifact_id=core_common.ArtifactId(root="facts", kind="artifact"),
                        digest="old-facts",
                    )
                ],
                output_schema=core_common.SchemaRef(name="reply", version="1"),
            ),
        ),
    )
    before = classify_context(request, contract="agent.v1")
    assert request.action is not None
    assert request.action.field == "turn"
    turn = request.action.value
    assert turn.session is not None
    turn.session.model = value
    turn.prompts[0].digest = value
    request.within_seconds = 31
    after = classify_context(request, contract="agent.v1")
    assert before.identity == after.identity
    assert before.content != after.content
    turn.artifact_dependencies[0].digest = value
    changed = classify_context(request, contract="agent.v1")
    assert changed.identity != before.identity
