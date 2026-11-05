"""Requests, steps, templates and the workload: what a task can build, and what it is told when it cannot."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from vibesys_evaluator.service import Operation, Request, Template, Workload, get, post, ref

if TYPE_CHECKING:
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
        pytest.param(lambda: Request(get("/x"), exclusive=""), "1 to 256", id="empty-exclusive"),
        pytest.param(lambda: Request(*[get("/x")] * 17), "1 to 16 steps", id="many-steps"),
    ],
)
def test_invalid_requests_are_refused_naming_the_part(build: object, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        build()  # type: ignore[operator]


def test_a_request_takes_only_steps() -> None:
    with pytest.raises(TypeError, match="not a Step"):
        Request("/x")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("operation", "match"),
    [
        pytest.param({"name": "Read", "weight": 1, "build": _build}, "name", id="name"),
        pytest.param({"name": "read", "weight": 0, "build": _build}, "weight", id="zero-weight"),
        pytest.param(
            {"name": "read", "weight": float("inf"), "build": _build}, "weight", id="inf-weight"
        ),
        pytest.param(
            {"name": "read", "weight": 1, "build": _build, "sample": 1.5},
            "sample",
            id="sample-range",
        ),
        pytest.param(
            {
                "name": "write",
                "weight": 1,
                "build": _build,
                "apply": lambda _m, _c: None,
                "sample": 0.5,
            },
            "always checked",
            id="write-sample",
        ),
    ],
)
def test_invalid_operations_are_refused(operation: dict, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        Operation(**operation)


def test_a_workload_needs_distinct_operations() -> None:
    read = Operation(name="read", weight=1, build=_build)
    with pytest.raises(ValueError, match="at least one"):
        Workload(model=lambda d, _r: d, operations=[])
    with pytest.raises(ValueError, match="two operations named 'read'"):
        Workload(model=lambda d, _r: d, operations=[read, read])
