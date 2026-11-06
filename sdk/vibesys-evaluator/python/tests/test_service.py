"""Requests, steps, templates and the workload: what a task can build, and what it is told when it cannot."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

# test-isolation: a Step's message is what the plan carries; the helper reads it.
from vibesys_evaluator._private.http import step_message
from vibesys_evaluator._proto.vibesys.evaluator.v1 import loadgen_pb as pb
from vibesys_evaluator.microservices import (
    Operation,
    Request,
    Template,
    Workload,
    get,
    http,
    post,
    put,
    ref,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from vibesys_evaluator import Rand


def _build(_m: object, _rand: Rand) -> Request:
    return Request(get("/x"))


def test_a_ref_needs_an_earlier_capture() -> None:
    use = get("/kv", headers={"Authorization": "Bearer " + ref("token")})
    with pytest.raises(ValueError, match=r"step 0 refers to \['token'\]"):
        Request(use)
    with pytest.raises(ValueError, match=r"step 1 refers to \['token'\]"):
        Request(post("/login", capture={"other": "/token"}), use)
    Request(post("/login", capture={"token": "/token"}), use)


def test_templates_concatenate_both_ways() -> None:
    template = "a" + ref("x") + "b" + ref("y")
    assert isinstance(template, Template)
    assert repr(template) == "Template('a{x}b{y}')"
    assert repr(ref("x") + ref("y")) == "Template('{x}{y}')"


@pytest.mark.parametrize(
    ("build", "match"),
    [
        pytest.param(lambda: get("kv"), "must start with '/'", id="relative-path"),
        pytest.param(lambda: get(ref("p")), "literal '/'", id="ref-path"),
        pytest.param(
            lambda: post("/x", form={"a": "1"}, json={"a": 1}), "at most one", id="two-bodies"
        ),
        pytest.param(lambda: get("/x", expect=99), "100..599", id="status"),
        pytest.param(lambda: get("/x", expect=()), "1 to 16", id="no-status"),
        pytest.param(lambda: get("/x", capture={"Token": "/t"}), "capture name", id="capture-name"),
        pytest.param(lambda: get("/x", capture={"t": "token"}), "JSON pointer", id="pointer"),
        pytest.param(lambda: ref("Bad"), "ref name", id="ref-name"),
        pytest.param(lambda: post("/x", json={"n": float("nan")}), "not JSON", id="json-nan"),
        pytest.param(Request, "1 to 16 steps", id="no-steps"),
        pytest.param(lambda: http("TRACE", "/x"), "not one of GET, POST", id="method"),
        pytest.param(lambda: http("DELETE", "x"), "must start with '/'", id="http-path"),
        pytest.param(lambda: Request(*[get("/x")] * 17), "1 to 16 steps", id="many-steps"),
    ],
)
def test_invalid_requests_are_refused_naming_the_part(
    build: Callable[[], object], match: str
) -> None:
    with pytest.raises(ValueError, match=match):
        build()


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("GET", pb.HttpMethod.GET),
        ("post", pb.HttpMethod.POST),
        ("Put", pb.HttpMethod.PUT),
        ("DELETE", pb.HttpMethod.DELETE),
        ("patch", pb.HttpMethod.PATCH),
    ],
)
def test_http_sends_any_method_the_schema_has(method: str, expected: pb.HttpMethod) -> None:
    message = step_message(http(method, "/kv/k1", body=b"v", expect=(200, 204)))
    assert message.call is not None
    call = message.call.value
    assert isinstance(call, pb.HttpCall)
    assert call.method == expected
    assert call.body is not None
    assert call.body.value == b"v"
    assert list(message.expect_status) == [200, 204]


def test_the_shorthands_are_http_of_their_method() -> None:
    assert step_message(get("/x", query={"a": "1"})) == step_message(
        http("GET", "/x", query={"a": "1"})
    )
    assert step_message(post("/x", json={"a": 1})) == step_message(
        http("POST", "/x", json={"a": 1})
    )
    assert step_message(put("/x", form={"a": "1"})) == step_message(
        http("PUT", "/x", form={"a": "1"})
    )


def test_a_request_takes_only_steps() -> None:
    with pytest.raises(TypeError, match="not a Step"):
        Request("/x")  # ty: ignore[invalid-argument-type]


@pytest.mark.parametrize(
    ("operation", "match"),
    [
        pytest.param({"name": "Read", "weight": 1, "build": _build}, "name", id="name"),
        pytest.param(
            {"name": "config.create", "weight": 1, "build": _build},
            "'config.create' is not one loadgen takes",
            id="dotted-name",
        ),
        pytest.param({"name": "read", "weight": 0, "build": _build}, "weight", id="zero-weight"),
        pytest.param(
            {"name": "read", "weight": float("inf"), "build": _build}, "weight", id="inf-weight"
        ),
    ],
)
def test_invalid_operations_are_refused(operation: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        Operation(**operation)


@pytest.mark.parametrize("removed", ["check", "apply", "sample"])
def test_an_operation_has_no_benchmark_time_check(removed: str) -> None:
    # A benchmark only measures; an accuracy check judges the responses (docs/spec.md).
    with pytest.raises(TypeError, match=removed):
        Operation(name="read", weight=1, build=_build, **{removed: None})


def test_a_workload_needs_distinct_operations() -> None:
    read = Operation(name="read", weight=1, build=_build)
    with pytest.raises(ValueError, match="at least one"):
        Workload(model=lambda d, _r: d, operations=[])
    with pytest.raises(ValueError, match="two operations are named 'read'"):
        Workload(model=lambda d, _r: d, operations=[read, read])
