"""Agent runner helpers: run_agent, run_implementer_agent, run_judge_agent, run_perf_eval_agent."""

import json
import re
import uuid

from pydantic import ValidationError

from vibeserve_agent.agents.callbacks import AgentLogger, TodoDisplay
from vibeserve_agent.schemas import (
    ImplementerResponse,
    IssueImplementerResponse,
    IssueJudgeResponse,
    IssuePerfEvalResponse,
    JudgeResponse,
    PerfEvalResponse,
    PerfMetrics,
    PerfTrend,
    ProfilerResponse,
    Verdict,
)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_MAX_TEXT_LEN = 2000
_JUDGE_REVIEW_PROMPT = (
    "Review the implementation. "
    "Write or update pytest tests, run them via `uv run pytest -v`, "
    "and reflect on the results before giving your verdict. "
    "Return exactly one raw JSON object with keys "
    '"analysis", "feedback", and "verdict". '
    'Do not use markdown fences or any extra text.'
)


_PROFILER_PROMPT = (
    "Profile the implementation with nsys and analyze the results. "
    "Return exactly one raw JSON object with keys "
    '"analysis", "bottlenecks", and "suggestions". '
    "Do not use markdown fences or any extra text."
)


# ---------------------------------------------------------------------------
# Stream update helpers
# ---------------------------------------------------------------------------


def _extract_todos(update: dict) -> list[dict] | None:
    """Extract todos from any node in a stream update."""
    for node_data in update.values():
        if isinstance(node_data, dict):
            todos = node_data.get("todos")
            if todos is not None:
                return todos
    return None


def _iter_update_dicts(value):
    """Yield all nested dict nodes reachable from a stream update payload."""
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from _iter_update_dicts(nested)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_update_dicts(item)


def _extract_text_from_message_content(content) -> str:
    """Normalize AI message content that may be a string or a content-block list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
        return "".join(parts)
    return ""


def _extract_last_ai_message_text(update: dict) -> str:
    """Find the most recent AI message text anywhere in the streamed update tree."""
    last_text = ""
    for node in _iter_update_dicts(update):
        messages = node.get("messages")
        if not isinstance(messages, list):
            continue
        for msg in messages:
            if getattr(msg, "type", None) != "ai":
                continue
            text = _extract_text_from_message_content(getattr(msg, "content", None))
            if text:
                last_text = text
    return last_text


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------


def _log_agent_config(agent, label: str, log_file) -> None:
    """Write agent configuration (tools list) to log file."""
    if not log_file:
        return
    log_file.write(f"\n{'='*60}\n")
    log_file.write(f"  Agent Configuration: {label}\n")
    log_file.write(f"{'='*60}\n")

    # Extract tools
    try:
        tools_node = agent.builder.nodes["tools"]
        tools_dict = tools_node.runnable.tools_by_name
        log_file.write(f"\n  Tools ({len(tools_dict)}):\n")
        for name, tool in sorted(tools_dict.items()):
            desc = getattr(tool, "description", "")
            # Truncate long descriptions to first line
            first_line = desc.split("\n")[0] if desc else ""
            log_file.write(f"    - {name}: {first_line}\n")
    except (KeyError, AttributeError):
        log_file.write("  Tools: <unable to extract>\n")

    log_file.write("\n")
    log_file.flush()


def _log_and_print(
    text: str,
    log_file=None,
    max_len: int | None = None,
) -> None:
    """Print to stdout (optionally truncated) and write full text to log_file."""
    if log_file:
        log_file.write(text + "\n")
        log_file.flush()
    if max_len is not None and len(text) > max_len:
        print(text[:max_len])
        print(f"... [{len(text) - max_len} more chars, see log for full text]")
    else:
        print(text)


# ---------------------------------------------------------------------------
# Generic typed-response parsing
# ---------------------------------------------------------------------------


def _parse_typed_response_text(text: str, response_cls):
    """Best-effort recovery of a typed Pydantic payload from raw model text."""
    if not text:
        return None

    candidates: list[str] = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)

    fenced_matches = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidates.extend(match.strip() for match in fenced_matches if match.strip())

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start : end + 1].strip())

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            payload = json.loads(candidate)
            return response_cls.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, TypeError):
            continue
    return None


def _coerce_typed_response(payload, response_cls):
    if isinstance(payload, response_cls):
        return payload
    if isinstance(payload, dict):
        try:
            return response_cls(**payload)
        except ValidationError:
            return None
    if isinstance(payload, str):
        return _parse_typed_response_text(payload, response_cls)
    return None


def _extract_typed_structured_response(update, response_cls):
    """Find a structured response of the given type anywhere in the streamed update tree."""
    for node in _iter_update_dicts(update):
        if "structured_response" not in node:
            continue
        response = _coerce_typed_response(node["structured_response"], response_cls)
        if response is not None:
            return response
    return None


# Backwards-compatible aliases retained for tests that import these names
# directly. Internal callers should use the generic helpers above.
def _parse_implementer_response_text(text: str) -> ImplementerResponse | None:
    return _parse_typed_response_text(text, ImplementerResponse)


def _parse_perf_eval_response_text(text: str) -> PerfEvalResponse | None:
    return _parse_typed_response_text(text, PerfEvalResponse)


# ---------------------------------------------------------------------------
# Agent runners
# ---------------------------------------------------------------------------


def run_agent(
    agent,
    prompt: str,
    callbacks: list | None = None,
    thread_id: str | None = None,
    round_label: str = "implementer",
    log_file=None,
    max_text_len: int | None = None,
) -> str:
    """Run agent, return final AI message text.

    When *log_file* is provided, full input/output/error text is written there
    while stdout receives a truncated version (controlled by *max_text_len*).
    """
    if callbacks is None:
        callbacks = [AgentLogger()]
    callbacks_label = " + ".join(type(cb).__name__ for cb in callbacks)
    if thread_id is None:
        thread_id = uuid.uuid4().hex
    todo_display = TodoDisplay()
    _log_and_print(f"\n=== LLM ROUND START: {round_label} ===", log_file)
    _log_and_print(f"callbacks: {callbacks_label}", log_file)
    _log_and_print(f"thread_id: {thread_id}", log_file)
    _log_and_print("--- input ---", log_file)
    _log_and_print(prompt, log_file, max_len=max_text_len)
    last_ai_message = ""
    try:
        for update in agent.stream(
            {"messages": [("human", prompt)]},
            config={"callbacks": callbacks, "configurable": {"thread_id": thread_id}},
            stream_mode="updates",
        ):
            todos = _extract_todos(update)
            if todos is not None:
                todo_display.update(todos)
            if "agent" in update:
                for msg in update["agent"].get("messages", []):
                    if getattr(msg, "type", None) == "ai" and msg.content:
                        last_ai_message = msg.content
    except Exception as exc:
        error_text = f"error: {type(exc).__name__}: {exc}"
        _log_and_print(f"\n=== LLM ROUND ERROR: {round_label} ===", log_file)
        _log_and_print(error_text, log_file, max_len=max_text_len)
        raise
    output_text = last_ai_message if last_ai_message else "<no ai message returned>"
    _log_and_print("\n=== LLM ROUND OUTPUT (final ai message) ===", log_file)
    _log_and_print(output_text, log_file, max_len=max_text_len)
    return last_ai_message


def _run_typed_agent(
    agent,
    prompt: str,
    *,
    response_cls,
    label: str,
    fallback_factory,
    callbacks: list | None = None,
    thread_id: str | None = None,
    round_label: str = "agent",
    log_file=None,
    max_text_len: int | None = None,
):
    """Generic agent runner returning a structured Pydantic response of *response_cls*.

    All structured-response runners share this plumbing so the per-agent
    wrappers stay tiny: they only need to specify the response class, the log
    label, and a fallback constructor for when no structured response arrives.
    """
    if callbacks is None:
        callbacks = [AgentLogger()]
    if thread_id is None:
        thread_id = uuid.uuid4().hex
    todo_display = TodoDisplay()
    callbacks_label = " + ".join(type(cb).__name__ for cb in callbacks)
    structured_response = None
    last_ai_message = ""
    _log_and_print(f"\n=== {label} ROUND START: {round_label} ===", log_file)
    _log_and_print(f"callbacks: {callbacks_label}", log_file)
    _log_and_print(f"thread_id: {thread_id}", log_file)
    _log_and_print("--- input ---", log_file)
    _log_and_print(prompt, log_file, max_len=max_text_len)
    try:
        for update in agent.stream(
            {"messages": [("human", prompt)]},
            config={"callbacks": callbacks, "configurable": {"thread_id": thread_id}},
            stream_mode="updates",
        ):
            todos = _extract_todos(update)
            if todos is not None:
                todo_display.update(todos)
            structured_response = (
                _extract_typed_structured_response(update, response_cls) or structured_response
            )
            extracted_text = _extract_last_ai_message_text(update)
            if extracted_text:
                last_ai_message = extracted_text
    except Exception as exc:
        error_text = f"error: {type(exc).__name__}: {exc}"
        _log_and_print(f"\n=== {label} ROUND ERROR: {round_label} ===", log_file)
        _log_and_print(error_text, log_file, max_len=max_text_len)
        raise
    if structured_response is None:
        structured_response = _parse_typed_response_text(last_ai_message, response_cls)
    if structured_response is None:
        _log_and_print(f"\n=== {label} ROUND OUTPUT (missing response) ===", log_file)
        _log_and_print(f"No structured response received from {label.lower()}.", log_file)
        if last_ai_message:
            _log_and_print(f"\n=== {label} ROUND OUTPUT (raw ai message) ===", log_file)
            _log_and_print(last_ai_message, log_file, max_len=max_text_len)
        return fallback_factory()
    output_json = structured_response.model_dump_json(indent=2)
    _log_and_print(f"\n=== {label} ROUND OUTPUT ===", log_file)
    _log_and_print(output_json, log_file, max_len=max_text_len)
    return structured_response


def run_implementer_agent(
    agent,
    prompt: str,
    callbacks: list | None = None,
    thread_id: str | None = None,
    round_label: str = "implementer",
    log_file=None,
    max_text_len: int | None = None,
) -> ImplementerResponse:
    """Run implementer agent, return structured ImplementerResponse."""
    return _run_typed_agent(
        agent,
        prompt,
        response_cls=ImplementerResponse,
        label="IMPLEMENTER",
        fallback_factory=lambda: ImplementerResponse(
            summary="Implementer did not produce a structured response.",
            expected_behavior="Unknown.",
        ),
        callbacks=callbacks,
        thread_id=thread_id,
        round_label=round_label,
        log_file=log_file,
        max_text_len=max_text_len,
    )


def run_judge_agent(
    agent,
    prompt: str,
    callbacks: list | None = None,
    thread_id: str | None = None,
    round_label: str = "judge",
    log_file=None,
    max_text_len: int | None = None,
) -> JudgeResponse:
    """Run judge agent, return structured JudgeResponse.

    When *log_file* is provided, full input/output/error text is written there
    while stdout receives a truncated version (controlled by *max_text_len*).
    """
    return _run_typed_agent(
        agent,
        prompt,
        response_cls=JudgeResponse,
        label="JUDGE",
        fallback_factory=lambda: JudgeResponse(
            analysis="No structured response received from judge.",
            feedback="Judge did not produce a structured response.",
            verdict=Verdict.FAIL,
        ),
        callbacks=callbacks,
        thread_id=thread_id,
        round_label=round_label,
        log_file=log_file,
        max_text_len=max_text_len,
    )


def run_perf_eval_agent(
    agent,
    prompt: str,
    callbacks: list | None = None,
    thread_id: str | None = None,
    round_label: str = "perf_eval",
    log_file=None,
    max_text_len: int | None = None,
) -> PerfEvalResponse:
    """Run perf evaluator agent, return structured PerfEvalResponse."""
    return _run_typed_agent(
        agent,
        prompt,
        response_cls=PerfEvalResponse,
        label="PERF EVAL",
        fallback_factory=lambda: PerfEvalResponse(
            analysis="No structured response received from perf evaluator.",
            metrics=PerfMetrics(load_levels=[]),
            implementer_feedback=[],
            evaluator_feedback=[],
            throughput_trend=PerfTrend.MIXED,
            latency_trend=PerfTrend.MIXED,
        ),
        callbacks=callbacks,
        thread_id=thread_id,
        round_label=round_label,
        log_file=log_file,
        max_text_len=max_text_len,
    )


# ---------------------------------------------------------------------------
# Profiler response parsing
# ---------------------------------------------------------------------------


def _parse_profiler_response_text(text: str) -> ProfilerResponse | None:
    """Best-effort recovery when the model emits raw JSON instead of a typed payload."""
    if not text:
        return None

    candidates: list[str] = []
    stripped = text.strip()
    if stripped:
        candidates.append(stripped)

    fenced_matches = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=re.DOTALL)
    candidates.extend(match.strip() for match in fenced_matches if match.strip())

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start : end + 1].strip())

    seen: set[str] = set()
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            payload = json.loads(candidate)
            return ProfilerResponse.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, TypeError):
            continue
    return None


def _coerce_profiler_response(payload) -> ProfilerResponse | None:
    if isinstance(payload, ProfilerResponse):
        return payload
    if isinstance(payload, dict):
        try:
            return ProfilerResponse(**payload)
        except ValidationError:
            return None
    if isinstance(payload, str):
        return _parse_profiler_response_text(payload)
    return None


def _extract_profiler_structured_response(update: dict) -> ProfilerResponse | None:
    """Find a structured profiler response anywhere in the streamed update tree."""
    for node in _iter_update_dicts(update):
        if "structured_response" not in node:
            continue
        response = _coerce_profiler_response(node["structured_response"])
        if response is not None:
            return response
    return None


# ---------------------------------------------------------------------------
# Profiler agent runner
# ---------------------------------------------------------------------------


def run_profiler_agent(
    agent,
    prompt: str,
    callbacks: list | None = None,
    thread_id: str | None = None,
    round_label: str = "profiler",
    log_file=None,
    max_text_len: int | None = None,
) -> ProfilerResponse:
    """Run profiler agent, return structured ProfilerResponse."""
    if callbacks is None:
        callbacks = [AgentLogger()]
    if thread_id is None:
        thread_id = uuid.uuid4().hex
    todo_display = TodoDisplay()
    callbacks_label = " + ".join(type(cb).__name__ for cb in callbacks)
    structured_response = None
    last_ai_message = ""
    _log_and_print(f"\n=== PROFILER ROUND START: {round_label} ===", log_file)
    _log_and_print(f"callbacks: {callbacks_label}", log_file)
    _log_and_print(f"thread_id: {thread_id}", log_file)
    _log_and_print("--- input ---", log_file)
    _log_and_print(prompt, log_file, max_len=max_text_len)
    try:
        for update in agent.stream(
            {"messages": [("human", prompt)]},
            config={"callbacks": callbacks, "configurable": {"thread_id": thread_id}},
            stream_mode="updates",
        ):
            todos = _extract_todos(update)
            if todos is not None:
                todo_display.update(todos)
            structured_response = _extract_profiler_structured_response(update) or structured_response
            extracted_text = _extract_last_ai_message_text(update)
            if extracted_text:
                last_ai_message = extracted_text
    except Exception as exc:
        error_text = f"error: {type(exc).__name__}: {exc}"
        _log_and_print(f"\n=== PROFILER ROUND ERROR: {round_label} ===", log_file)
        _log_and_print(error_text, log_file, max_len=max_text_len)
        raise
    if structured_response is None:
        structured_response = _parse_profiler_response_text(last_ai_message)
    if structured_response is None:
        _log_and_print(f"\n=== PROFILER ROUND OUTPUT (missing response) ===", log_file)
        _log_and_print("No structured response received from profiler.", log_file)
        if last_ai_message:
            _log_and_print("\n=== PROFILER ROUND OUTPUT (raw ai message) ===", log_file)
            _log_and_print(last_ai_message, log_file, max_len=max_text_len)
        return ProfilerResponse(
            analysis="No structured response received from profiler.",
            bottlenecks="Profiler did not produce a structured response.",
            suggestions="Re-run profiling manually.",
        )
    output_json = structured_response.model_dump_json(indent=2)
    _log_and_print("\n=== PROFILER ROUND OUTPUT ===", log_file)
    _log_and_print(output_json, log_file, max_len=max_text_len)
    return structured_response


def run_issue_implementer_agent(
    agent,
    prompt: str,
    *,
    issue_id: int,
    callbacks: list | None = None,
    thread_id: str | None = None,
    round_label: str = "issue_implementer",
    log_file=None,
    max_text_len: int | None = None,
) -> IssueImplementerResponse:
    """Run the issue-loop implementer agent, return structured IssueImplementerResponse."""
    return _run_typed_agent(
        agent,
        prompt,
        response_cls=IssueImplementerResponse,
        label="ISSUE IMPLEMENTER",
        fallback_factory=lambda: IssueImplementerResponse(
            issue_id=issue_id,
            summary="Implementer did not produce a structured response.",
            files_touched=[],
            self_check="Unknown — no structured response.",
        ),
        callbacks=callbacks,
        thread_id=thread_id,
        round_label=round_label,
        log_file=log_file,
        max_text_len=max_text_len,
    )


def run_issue_judge_agent(
    agent,
    prompt: str,
    *,
    issue_id: int,
    callbacks: list | None = None,
    thread_id: str | None = None,
    round_label: str = "issue_judge",
    log_file=None,
    max_text_len: int | None = None,
) -> IssueJudgeResponse:
    """Run the issue-loop judge agent, return structured IssueJudgeResponse."""
    return _run_typed_agent(
        agent,
        prompt,
        response_cls=IssueJudgeResponse,
        label="ISSUE JUDGE",
        fallback_factory=lambda: IssueJudgeResponse(
            issue_id=issue_id,
            analysis="No structured response received from judge.",
            feedback="Judge did not produce a structured response.",
            verdict=Verdict.FAIL,
            new_issues_filed=[],
        ),
        callbacks=callbacks,
        thread_id=thread_id,
        round_label=round_label,
        log_file=log_file,
        max_text_len=max_text_len,
    )


def run_issue_perf_eval_agent(
    agent,
    prompt: str,
    callbacks: list | None = None,
    thread_id: str | None = None,
    round_label: str = "issue_perf_eval",
    log_file=None,
    max_text_len: int | None = None,
) -> IssuePerfEvalResponse:
    """Run the issue-loop performance evaluator agent, return structured IssuePerfEvalResponse."""
    return _run_typed_agent(
        agent,
        prompt,
        response_cls=IssuePerfEvalResponse,
        label="ISSUE PERF EVAL",
        fallback_factory=lambda: IssuePerfEvalResponse(
            analysis="No structured response received from perf evaluator.",
            metrics=PerfMetrics(load_levels=[]),
            evaluator_feedback=[],
            new_issue_ids=[],
            throughput_trend=PerfTrend.MIXED,
            latency_trend=PerfTrend.MIXED,
        ),
        callbacks=callbacks,
        thread_id=thread_id,
        round_label=round_label,
        log_file=log_file,
        max_text_len=max_text_len,
    )
