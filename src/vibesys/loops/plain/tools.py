"""Issue-tracker tools exposed to issue-loop agents.

The factory ``build_issue_tools`` returns a list of @tool-decorated callables
bound to a specific :class:`IssueBoard`, iteration, and creator identity.
The per-iteration cap on ``create_issue`` is enforced server-side by reading
the store, NOT via a closure-mutable counter — this keeps resume-after-crash
trivial because the cap re-derives correctly from persisted state.

Subset routing (chosen by the loop):

- Implementer: NO issue tools at all (the issue is inlined in its prompt).
- Judge: read tools + ``create_issue`` with creator='judge', cap=1, type='bug'.
- Perf_eval: read tools + ``create_issue`` with creator='perf_eval', cap=N,
  all three types allowed.

The actual logic for parsing, validation, store mutation, and formatting lives
in :mod:`vs_issue_board`. The ``@tool`` callables here are thin shells that
adapt those generic helpers to LangChain.
"""

from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from vs_issue_board import (
    CreateIssuePolicy,
    IssueBoard,
    IssueStatus,
    IssueType,
    create_issue_under_policy,
    format_issue_full,
    format_issue_short,
)


def build_issue_tools(
    store: IssueBoard,
    *,
    iteration: int,
    can_create: bool = False,
    creator: str = "agent",
    create_cap: int | None = None,
    allowed_create_types: set[IssueType] | None = None,
) -> list[BaseTool]:
    """Return tracker tools bound to *store*, *iteration*, and *creator*.

    Args:
        store: The issue store to read/write.
        iteration: 1-based outer iteration. Used to enforce the per-iteration
            ``create_issue`` cap.
        can_create: If True, include the ``create_issue`` tool.
        creator: The actor string recorded on newly created issues.
        create_cap: Maximum number of issues *creator* may create in
            *iteration*. ``None`` = unlimited.
        allowed_create_types: Set of issue types the creator may file.
            ``None`` = all types.
    """

    @tool
    def list_issues(status: str | None = None) -> str:
        """List issues. Optional status filter: 'open', 'in_progress', 'closed', 'blocked'."""
        store.reload()
        try:
            status_enum = IssueStatus(status) if status else None
        except ValueError:
            return (
                f"error: invalid status '{status}'. "
                f"Use one of: {[s.value for s in IssueStatus]} or omit."
            )
        issues = store.list(status=status_enum)
        if not issues:
            return "(no issues)"
        return "\n".join(format_issue_short(i) for i in issues)

    @tool
    def get_issue(issue_id: int) -> str:
        """Return the full body of an issue by id."""
        store.reload()
        issue = store.get(issue_id)
        if issue is None:
            return f"(no issue #{issue_id})"
        return format_issue_full(issue)

    @tool
    def search_issues(query: str) -> str:
        """Substring search across all issues' title+description.

        Use comma-separated keywords for AND-matching, e.g. 'kv cache, paged'.
        Matching is case-insensitive.
        """
        store.reload()
        hits = store.search(query)
        if not hits:
            return "(no matches)"
        return "\n".join(format_issue_short(i) for i in hits)

    tools: list[BaseTool] = [list_issues, get_issue, search_issues]

    if can_create:
        policy = CreateIssuePolicy(
            creator=creator,
            iteration=iteration,
            cap=create_cap,
            allowed_types=frozenset(
                allowed_create_types if allowed_create_types is not None else IssueType
            ),
        )

        @tool
        def create_issue(type: str, title: str, description: str) -> str:
            """Create a new issue.

            Args:
                type: One of 'bug', 'feature', 'perf'.
                title: Short summary (one line).
                description: Markdown body. Should follow this template:
                  ## Background
                  ## Acceptance criteria
                  ## Notes
            """
            _, msg = create_issue_under_policy(
                store,
                type_str=type,
                title=title,
                description=description,
                policy=policy,
            )
            return msg

        tools.append(create_issue)

    return tools
