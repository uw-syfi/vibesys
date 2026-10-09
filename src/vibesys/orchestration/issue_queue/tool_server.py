"""Issue-board tool declarations driven by plugin-owned policy state.

This module owns what the four issue-board tools accept and return. The process
that serves them (argument parsing, reading the policy and tracker files,
opening the tracker, speaking MCP) lives in
``entrypoints.issue_board_tools_server``. Handlers reach the tracker and the
creation policy only through an :class:`IssueBoardContext`, so each call reads
the current contents the orchestration has written.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from vibesys.orchestration.issue_queue.prompts import (
    create_issue_result,
    invalid_status_result,
    issue_list_result,
    issue_result,
)
from vs_issue_tracker.api import (
    CreateIssuePolicy,
    IssueStatus,
    IssueTracker,
    IssueType,
    create_issue_under_policy,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys.orchestration.issue_queue.models import IssueToolPolicy


@dataclass(frozen=True, slots=True)
class IssueBoardContext:
    """How a served tool reaches the issue board and the current creation policy."""

    board: Callable[[], IssueTracker]
    policy: Callable[[], IssueToolPolicy]


@dataclass(frozen=True, slots=True)
class IssueBoardTool:
    """One tool: its name, description, argument schema and handler."""

    name: str
    description: str
    input_schema: type[BaseModel]
    handler: Callable[[Any], str]


class _ListIssuesArgs(BaseModel):
    status: str | None = None


class _GetIssueArgs(BaseModel):
    issue_id: int


class _SearchIssuesArgs(BaseModel):
    query: str


class _CreateIssueArgs(BaseModel):
    type: str
    title: str
    description: str


def _create_policy(configured: IssueToolPolicy) -> CreateIssuePolicy:
    return CreateIssuePolicy(
        creator=configured.creator,
        iteration=configured.iteration,
        cap=configured.cap,
        allowed_types=frozenset(IssueType(value) for value in configured.allowed_types),
    )


def issue_board_tools(context: IssueBoardContext) -> tuple[IssueBoardTool, ...]:
    """Declare issue queries and policy-checked creation over ``context``."""

    def list_issues(args: _ListIssuesArgs) -> str:
        try:
            selected = IssueStatus(args.status) if args.status else None
        except ValueError:
            return invalid_status_result(args.status or "")
        return issue_list_result(context.board().list(status=selected), searched=False)

    def get_issue(args: _GetIssueArgs) -> str:
        return issue_result(args.issue_id, context.board().get(args.issue_id))

    def search_issues(args: _SearchIssuesArgs) -> str:
        return issue_list_result(context.board().search(args.query), searched=True)

    def create_issue(args: _CreateIssueArgs) -> str:
        return create_issue_result(
            create_issue_under_policy(
                context.board(),
                type_str=args.type,
                title=args.title,
                description=args.description,
                policy=_create_policy(context.policy()),
            )
        )

    return (
        IssueBoardTool(
            "list_issues",
            "List issues, optionally filtered by lifecycle status.",
            _ListIssuesArgs,
            list_issues,
        ),
        IssueBoardTool(
            "get_issue",
            "Return the complete issue with its history.",
            _GetIssueArgs,
            get_issue,
        ),
        IssueBoardTool(
            "search_issues",
            "Find issues containing every comma-separated, case-insensitive term.",
            _SearchIssuesArgs,
            search_issues,
        ),
        IssueBoardTool(
            "create_issue",
            "Create an issue when the current turn policy permits it.",
            _CreateIssueArgs,
            create_issue,
        ),
    )
