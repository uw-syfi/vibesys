"""Static product read handler registration for the descriptor-only service."""

from __future__ import annotations

from typing import TYPE_CHECKING

from vs_proto.api import host_operations
from vs_proto.api import host_payloads as pb

if TYPE_CHECKING:
    from vibesys.api.host_operations._reads import ReadSource

Handlers = host_operations.Handlers


class _Reads:
    def __init__(self, source: ReadSource) -> None:
        self._source = source

    def list_tasks(self, request: pb.ListTasksRequest) -> pb.ListTasksResponse:
        return pb.ListTasksResponse(
            request_id=request.request_id, tasks=self._source.tasks(request.project)
        )

    def list_harnesses(self, request: pb.ListHarnessesRequest) -> pb.ListHarnessesResponse:
        installed = self._source.installed_harnesses()
        listed = sorted(
            self._source.harnesses(), key=lambda option: option.provider not in installed
        )
        if not request.with_generations:
            for option in listed:
                if option.display is not None:
                    option.display.clear_field("maker")
                    option.display.clear_field("generation")
        return pb.ListHarnessesResponse(request_id=request.request_id, harnesses=listed)

    def list_examples(self, request: pb.ListExamplesRequest) -> pb.ListExamplesResponse:
        return pb.ListExamplesResponse(
            request_id=request.request_id,
            listing=self._source.examples(with_catalogue=request.with_catalogue is True),
        )

    def get_task_goal(self, request: pb.GetTaskGoalRequest) -> pb.GetTaskGoalResponse:
        return pb.GetTaskGoalResponse(
            request_id=request.request_id,
            goal=self._source.task_goal(request.project, request.task),
        )

    def describe_machine(self, request: pb.DescribeMachineRequest) -> pb.DescribeMachineResponse:
        return pb.DescribeMachineResponse(
            request_id=request.request_id, machine=self._source.machine()
        )

    def list_compute(self, request: pb.ListComputeRequest) -> pb.ListComputeResponse:
        return pb.ListComputeResponse(
            request_id=request.request_id, settings=self._source.compute()
        )

    def preflight_run(self, request: pb.PreflightRunRequest) -> pb.PreflightRunResponse:
        response = self._source.preflight(request)
        response.checks = [
            check
            for check in response.checks
            if request.with_project_clean or check.id != pb.CheckId.PROJECT_CLEAN
        ]
        if not request.with_roles:
            response.clear_field("roles")
        if not request.with_backend:
            response.clear_field("backend")
            for check in response.checks:
                if (
                    check.cause is not None
                    and check.cause.value is not None
                    and check.cause.value.field == "backend_unavailable"
                ):
                    check.clear_field("cause")
        return response


def handlers(source: ReadSource) -> host_operations.Handlers:
    """All required slots are statically bound to typed product handlers."""
    reads = _Reads(source)
    return Handlers(
        list_tasks=reads.list_tasks,
        list_harnesses=reads.list_harnesses,
        list_examples=reads.list_examples,
        get_task_goal=reads.get_task_goal,
        describe_machine=reads.describe_machine,
        list_compute=reads.list_compute,
        preflight_run=reads.preflight_run,
    )
