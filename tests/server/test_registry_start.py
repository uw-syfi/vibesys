"""The registry start body is the helper start, field for field."""

from __future__ import annotations

from server.host_helper_requests import registry_start_request
from vs_proto.api import Oneof
from vs_proto.api import host_payloads as pb


def test_a_registry_start_keeps_the_helper_fields() -> None:
    request = pb.StartRunRequest(
        request_id="request-a",
        project="/project",
        task="mpmc",
        args=["--model", "x"],
        harness=pb.StartRunHarness(provider="codex", model="gpt-a"),
        options=pb.StartRunOptions(
            rounds=4,
            results=pb.StartRunResults.PUBLISH_GITHUB,
            roles=[
                pb.StartRunRole(
                    role="judge",
                    model="gpt-b",
                    reasoning=pb.StartRunReasoning.HIGH,
                )
            ],
        ),
        agents=[
            pb.StartRunAgent(
                role="judge",
                harness=pb.StartRunHarness(provider="codex", model="gpt-b"),
            )
        ],
        measure_on=pb.StartRunMeasure(target=Oneof("slurm_cluster", "lab")),
    )

    body = registry_start_request(request)

    assert body.project == "/project"
    assert body.task == "mpmc"
    assert body.args == ("--model", "x")
    assert body.harness is not None
    assert (body.harness.provider, body.harness.model) == ("codex", "gpt-a")
    assert body.options is not None
    assert body.options.rounds == 4
    assert body.options.results == "publish_github"
    assert body.options.roles[0].role == "judge"
    assert body.options.roles[0].reasoning_effort == "high"
    assert body.agents is not None
    assert body.agents["judge"].model == "gpt-b"
    assert body.measure_on is not None
    assert body.measure_on.kind == "slurm"
    assert body.measure_on.cluster == "lab"
