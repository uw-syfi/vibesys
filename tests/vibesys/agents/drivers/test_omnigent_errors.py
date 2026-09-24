"""Failure-path and diagnostics tests for the Omnigent driver adapter."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from vs_agent.contracts import AgentExecutionPolicy, AgentSessionSpec
from vs_agent.drivers import omnigent as subject
from vs_agent.drivers.omnigent import (
    OmnigentDependencyError,
    OmnigentDriver,
    OmnigentDriverError,
    OmnigentSession,
)

pytest.importorskip("omnigent")

_Dynamic = Any


def _private(target: object, name: str) -> _Dynamic:
    """Reach a private module or object member the tests deliberately drive."""
    return getattr(target, name)


def _spec(tmp_path: Path) -> AgentSessionSpec:
    return replace(
        AgentSessionSpec(
            role="judge",
            provider="codex",
            workspace=tmp_path,
            model="gpt-5.5",
            policy=AgentExecutionPolicy(),
        )
    )


class _Executor:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        if self.error is not None:
            raise self.error


class _Resources:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        if self.error is not None:
            raise self.error


class _MCP:
    """Stands in for the native MCP owner during session shutdown."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.schemas: list[dict[str, Any]] = []
        self.close_calls = 0

    async def close(self) -> None:
        self.close_calls += 1
        if self.error is not None:
            raise self.error


def _session(
    driver: OmnigentDriver,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    **parts: object,
) -> OmnigentSession:
    built = (parts.get("executor") or _Executor(), [], parts.get("mcp"), parts.get("resources"))
    monkeypatch.setattr(driver, "_build_executor", lambda _spec: built)
    return driver.create_session(_spec(tmp_path))


@pytest.mark.parametrize(
    ("error", "fragments"),
    [
        (OmnigentDriverError.unsupported_provider("x", ["a", "b"]), ["'x'", "['a', 'b']"]),
        (OmnigentDriverError.unsupported_platform("plan9"), ["'plan9'", "sandbox backend"]),
        (OmnigentDriverError.helper_builder_missing(), ["build_helper_env", "0.10.0"]),
        (OmnigentDriverError.helper_environment_missing(), ["helper", "sandbox", "seam"]),
        (OmnigentDriverError.provider_environment_missing("codex"), ["'codex'", "environment"]),
        (
            OmnigentDriverError.sandbox_unavailable("linux_bwrap", OSError("no bwrap")),
            ["'linux_bwrap'", "no bwrap"],
        ),
        (OmnigentDriverError.os_environment_unavailable(Path("/w")), ["OS environment", "/w"]),
        (OmnigentDriverError.shell_environment_unavailable(Path("/w")), ["shell environment"]),
        (OmnigentDriverError.shell_tool_missing(), ["sys_os_shell"]),
        (OmnigentDriverError.executor_class_missing("mod", "Cls"), ["'mod'", "'Cls'"]),
        (OmnigentDriverError.host_resource_grants_unsupported(), ["host-resource grants"]),
        (
            OmnigentDriverError.host_resource_grants_require_agentshim(["/a"]),
            ["['/a']", "agentshim"],
        ),
        (OmnigentDriverError.container_execution_unsupported(), ["container execution"]),
        (
            OmnigentDriverError.read_only_paths_unsupported([Path("a/b")]),
            ["top-level dot paths", "['a/b']"],
        ),
    ],
)
def test_driver_error_constructors_name_the_offending_value(
    error: OmnigentDriverError, fragments: list[str]
) -> None:
    assert type(error) is OmnigentDriverError
    for fragment in fragments:
        assert fragment in str(error)


@pytest.mark.parametrize(
    ("build", "what"),
    [
        (OmnigentDependencyError.os_environment_tools, "Omnigent OS-environment tools"),
        (OmnigentDependencyError.executor_events, "Omnigent executor event types"),
        (OmnigentDependencyError.os_environment_datamodel, "Omnigent OS-environment datamodel"),
    ],
)
def test_dependency_errors_keep_import_detail_and_setup_guidance(
    build: _Dynamic, what: str
) -> None:
    error = build(ImportError("no module named foo"))

    assert isinstance(error, OmnigentDriverError)
    assert str(error).startswith(f"{what} is not importable (ImportError: no module named foo)")
    assert "uv sync" in str(error)
    executor = OmnigentDependencyError.executor_module("provider.mod", ImportError("boom"))
    assert str(executor).startswith("provider.mod is not importable (ImportError: boom)")


def test_unsupported_provider_lists_supported_providers() -> None:
    with pytest.raises(OmnigentDriverError, match="does not support agent provider 'nope'"):
        _private(subject, "_resolve_executor_spec")("nope")


@pytest.mark.parametrize(
    ("platform", "os_name", "expected"),
    [
        ("linux", "posix", "linux_bwrap"),
        ("darwin", "posix", "darwin_seatbelt"),
        ("win32", "nt", "windows_jobobject"),
    ],
)
def test_sandbox_backend_follows_platform(
    monkeypatch: pytest.MonkeyPatch, platform: str, os_name: str, expected: str
) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(subject.os, "name", os_name)

    assert _private(subject, "_sandbox_backend_for_platform")() == expected


def test_unknown_platform_has_no_sandbox_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "plan9")
    monkeypatch.setattr(subject.os, "name", "posix")

    with pytest.raises(OmnigentDriverError, match="no sandbox backend for platform 'plan9'"):
        _private(subject, "_sandbox_backend_for_platform")()


def test_provider_environment_seam_is_validated() -> None:
    adapt = _private(subject, "_adapt_provider_environment")

    with pytest.raises(OmnigentDriverError, match="'unknown' executor has no mutable env"):
        adapt(SimpleNamespace(), provider="unknown", environment={})
    with pytest.raises(OmnigentDriverError, match="'codex' executor has no mutable env"):
        adapt(SimpleNamespace(_env=None), provider="codex", environment={})
    executor = SimpleNamespace(_env={"A": "1"})
    adapt(executor, provider="codex", environment={"B": "2"})
    assert _private(executor, "_env") == {"A": "1", "B": "2"}


def test_helper_environment_seam_is_validated() -> None:
    with pytest.raises(OmnigentDriverError, match="seam"):
        _private(subject, "_adapt_helper_environment")(SimpleNamespace(), {})


@pytest.fixture
def fresh_helper_hook() -> _Dynamic:
    """Run with the one-shot helper-environment hook uninstalled, then restore it."""
    installed = _private(subject, "_helper_environment_hook_installed")
    was_set = installed.is_set()
    installed.clear()
    yield
    if was_set:
        installed.set()
    else:
        installed.clear()


def test_helper_hook_reports_missing_module_and_missing_builder(
    monkeypatch: pytest.MonkeyPatch, fresh_helper_hook: None
) -> None:
    del fresh_helper_hook
    install = _private(subject, "_install_helper_environment_hook")

    def missing(_name: str) -> object:
        message = "gone"
        raise ImportError(message)

    monkeypatch.setattr(subject, "import_module", missing)
    with pytest.raises(OmnigentDependencyError, match="OS-environment tools is not importable"):
        install()

    monkeypatch.setattr(subject, "import_module", lambda _name: SimpleNamespace())
    with pytest.raises(OmnigentDriverError, match="no callable 'build_helper_env' seam"):
        install()


def test_build_os_tools_reports_missing_apis_sandbox_failure_and_missing_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    build = _private(subject, "_build_os_tools")

    def missing(_name: str) -> object:
        message = "gone"
        raise ImportError(message)

    monkeypatch.setattr(subject, "import_module", missing)
    with pytest.raises(OmnigentDependencyError, match="OS-environment tools"):
        build(object(), tmp_path)

    outcome: list[object] = []

    def create(_spec: object) -> object:
        result = outcome[0]
        if isinstance(result, BaseException):
            raise result
        return result

    modules = {
        "omnigent.inner.os_env": SimpleNamespace(create_os_environment=create),
        "omnigent.tools.base": SimpleNamespace(ToolContext=object),
        "omnigent.tools.builtins.os_env": SimpleNamespace(build_os_env_tools=lambda *_: []),
    }
    monkeypatch.setattr(subject, "import_module", lambda name: modules[name])
    monkeypatch.setattr(sys, "platform", "linux")

    outcome.append(OSError("bwrap missing"))
    with pytest.raises(OmnigentDriverError, match="'linux_bwrap' sandbox on this host: bwrap"):
        build(object(), tmp_path)
    outcome[0] = None
    with pytest.raises(OmnigentDriverError, match="could not create a sandboxed OS environment"):
        build(object(), tmp_path)


def test_executor_resources_close_all_and_raise_first_failure() -> None:
    cleaned: list[str] = []

    class Scratch:
        def __init__(self, error: BaseException | None) -> None:
            self.error = error

        def cleanup(self) -> None:
            cleaned.append("scratch")
            if self.error is not None:
                raise self.error

    both = _private(subject, "_ExecutorResources")(
        scratch=Scratch(OSError("scratch")), os_tools=_Resources(RuntimeError("tools"))
    )
    with pytest.raises(RuntimeError, match="tools"):
        both.close()
    assert cleaned == ["scratch"]
    both.close()
    assert cleaned == ["scratch"]

    scratch_only = _private(subject, "_ExecutorResources")(scratch=Scratch(OSError("scratch")))
    with pytest.raises(OSError, match="scratch"):
        scratch_only.close()


def test_close_executor_attempts_resources_and_preserves_first_failure() -> None:
    driver = OmnigentDriver()
    try:
        executor = _Executor(RuntimeError("executor"))
        resources = cast("Any", _Resources(OSError("resources")))
        with pytest.raises(RuntimeError, match="executor"):
            driver.close_executor(executor, resources=resources)
        assert (executor.close_calls, resources.close_calls) == (1, 1)

        lone = cast("Any", _Resources(OSError("resources")))
        with pytest.raises(OSError, match="resources"):
            driver.close_executor(_Executor(), resources=lone)
        assert lone.close_calls == 1
    finally:
        driver.close()


def test_session_close_reports_executor_failure_and_still_closes_everything(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = OmnigentDriver()
    executor = _Executor(RuntimeError("executor failed"))
    mcp = _MCP(OSError("mcp failed"))
    resources = _Resources()
    session = _session(
        driver, tmp_path, monkeypatch, executor=executor, mcp=mcp, resources=resources
    )

    with pytest.raises(RuntimeError, match="executor failed"):
        session.close()
    driver.close()

    assert (executor.close_calls, mcp.close_calls, resources.close_calls) == (1, 1, 1)


def test_session_close_reports_mcp_and_resource_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = OmnigentDriver()
    mcp_session = _session(driver, tmp_path, monkeypatch, mcp=_MCP(OSError("mcp failed")))
    with pytest.raises(OSError, match="mcp failed"):
        mcp_session.close()

    resources = _Resources(ValueError("resources failed"))
    resource_session = _session(driver, tmp_path, monkeypatch, resources=resources)
    with pytest.raises(ValueError, match="resources failed"):
        resource_session.close()
    assert resources.close_calls == 1
    driver.close()


def test_session_close_survives_release_and_submit_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = OmnigentDriver()
    session = _session(driver, tmp_path, monkeypatch)
    monkeypatch.setattr(
        driver, "release_session", lambda _s: (_ for _ in ()).throw(KeyError("release"))
    )
    with pytest.raises(KeyError, match="release"):
        session.close()

    failing = _session(driver, tmp_path, monkeypatch)
    real_submit = driver.submit

    def failed_future(awaitable: _Dynamic) -> concurrent.futures.Future[Any]:
        awaitable.close()
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        future.set_exception(OSError("future failed"))
        return future

    monkeypatch.setattr(driver, "submit", failed_future)
    with pytest.raises(OSError, match="future failed"):
        failing.close()

    broken = _session(driver, tmp_path, monkeypatch)

    def refuse(awaitable: _Dynamic) -> concurrent.futures.Future[Any]:
        awaitable.close()
        message = "runtime gone"
        raise RuntimeError(message)

    monkeypatch.setattr(driver, "submit", refuse)
    with pytest.raises(RuntimeError, match="runtime gone"):
        broken.close()
    monkeypatch.setattr(driver, "submit", real_submit)
    monkeypatch.setattr(driver, "release_session", lambda _s: None)
    with contextlib.suppress(Exception):  # teardown of sessions whose close already failed
        driver.close()


def test_cancel_and_close_are_rejected_on_the_session_event_loop_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    driver = OmnigentDriver()
    session = _session(driver, tmp_path, monkeypatch)

    async def from_loop(action: str) -> None:
        getattr(session, action)()

    with pytest.raises(RuntimeError, match="cannot be cancelled from its event-loop thread"):
        driver.run_awaitable(from_loop("cancel"))
    with pytest.raises(RuntimeError, match="cannot be closed from its event-loop thread"):
        driver.run_awaitable(from_loop("close"))

    async def close_driver() -> None:
        driver.close()

    with pytest.raises(RuntimeError, match="driver cannot be closed from its event-loop"):
        driver.run_awaitable(close_driver())
    driver.close()


def test_closed_driver_rejects_new_sessions(tmp_path: Path) -> None:
    driver = OmnigentDriver()
    driver.close()

    with pytest.raises(RuntimeError, match="Omnigent driver is closed"):
        driver.create_session(_spec(tmp_path))


def test_driver_close_reraises_unexpected_owner_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    driver = OmnigentDriver()
    original = _private(driver, "_close_owned_resources")

    def failing() -> BaseException | None:
        original()
        message = "owner cleanup failed"
        raise RuntimeError(message)

    monkeypatch.setattr(driver, "_close_owned_resources", failing)

    with pytest.raises(RuntimeError, match="owner cleanup failed"):
        driver.close()


def test_executor_class_lookup_reports_missing_module_and_class() -> None:
    driver = OmnigentDriver()
    try:
        lookup = _private(driver, "_executor_class")
        spec = SimpleNamespace(module="omnigent", class_name="NoSuchExecutor")
        with pytest.raises(OmnigentDriverError, match="'omnigent' has no 'NoSuchExecutor'"):
            lookup(spec)

        spec = SimpleNamespace(module="omnigent_does_not_exist", class_name="X")
        with pytest.raises(OmnigentDependencyError, match="omnigent_does_not_exist is not"):
            lookup(spec)
    finally:
        driver.close()


def _missing_module(_name: str) -> object:
    message = "gone"
    raise ImportError(message)


def test_os_tool_builder_rejects_missing_shell_environment(tmp_path: Path) -> None:
    builder = _private(subject, "_OSToolBuilder")(
        create_environment=lambda _spec: None,
        build_tools=lambda _env: [],
        workspace=tmp_path,
        environment={},
        environments=[],
    )

    with pytest.raises(OmnigentDriverError, match="could not create a sandboxed shell"):
        builder.build(object(), object())


def test_turn_driver_and_os_env_builder_report_missing_omnigent_apis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(subject, "import_module", _missing_module)
    request = subject.AgentTurnRequest("prompt")

    with pytest.raises(OmnigentDependencyError, match="executor event types"):
        asyncio.run(
            _private(subject, "_drive_turn")(
                _Executor(), request=request, reasoning_effort=None, tool_schemas=[], observer=None
            )
        )
    driver = OmnigentDriver()
    try:
        with pytest.raises(OmnigentDependencyError, match="OS-environment datamodel"):
            _private(driver, "_build_os_env")(_spec(tmp_path))
    finally:
        driver.close()
