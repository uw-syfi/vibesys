"""A registry project change is the helper's project change."""

from pathlib import Path

from server.host_api import HelperErrorCode
from server.host_examples import CreatedProject
from server.host_helper_requests import ProjectLookupError, apply_registered_project
from vs_proto.api import host_payloads as pb


class _Directories:
    def expand(self, text: str) -> str:
        return text

    def home(self) -> str:
        return "/home"


class _Examples:
    def __init__(self, *, taken: bool = False) -> None:
        self.created: list[tuple[str, Path]] = []
        self.taken = taken

    def create(self, example: str, destination: Path) -> CreatedProject:
        if self.taken:
            message = "already there"
            raise ProjectLookupError(HelperErrorCode.DESTINATION_EXISTS, str(destination), message)
        self.created.append((example, destination))
        return CreatedProject(project=str(destination), tasks=("a",))


class _Host:
    def __init__(self, examples: _Examples) -> None:
        self.examples = examples
        self.directories = _Directories()


def _request() -> pb.CreateExampleRequest:
    return pb.CreateExampleRequest(
        request_id="request-a", example="queue", destination="/work/queue"
    )


def test_a_registry_create_keeps_the_helper_project() -> None:
    examples = _Examples()

    response = apply_registered_project("create_example", _request(), _Host(examples))

    assert examples.created == [("queue", Path("/work/queue"))]
    assert response.result is not None
    assert response.result.field == "created"
    assert response.result.value.project == "/work/queue"
    assert list(response.result.value.tasks) == ["a"]


def test_a_taken_destination_is_the_create_answer() -> None:
    response = apply_registered_project("create_example", _request(), _Host(_Examples(taken=True)))

    assert response.result is not None
    assert response.result.field == "taken"
