from __future__ import annotations

import platform

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vibesys.domains.base import DomainName
from vibesys.profilers import (
    ACTIVE_PROFILER_KINDS,
    PROFILER_DEFINITIONS,
    ProfilerDefinition,
    ProfilerKind,
    allowed_profiler_kinds,
    coerce_profiler_kind,
    preflight_profiler_kind,
    resolve_profiler_kind,
)

_DOMAINS = tuple(DomainName)
_REQUESTED = tuple(ProfilerKind)
_BACKEND_KINDS = (None, *sorted(ACTIVE_PROFILER_KINDS, key=lambda kind: kind.value))
_ENVIRONMENT_DEFAULTS = (
    ProfilerKind.NONE,
    *sorted(ACTIVE_PROFILER_KINDS, key=lambda kind: kind.value),
)
_PROFILER_VALUES = frozenset(kind.value for kind in ProfilerKind)


def test_profiler_definitions_derive_uniform_packaging_names():
    assert frozenset(PROFILER_DEFINITIONS) == ACTIVE_PROFILER_KINDS
    for kind, definition in PROFILER_DEFINITIONS.items():
        assert definition.support_name == f"{kind.value}_profiler"
        assert definition.server_path == f"{kind.value}_profiler/server.py"
        assert definition.prompt_template == f"profilers/{kind.value}.j2"
        assert definition.mcp_name == f"vibesys-{kind.value.replace('_', '-')}-profiler"


def test_profiler_definition_needs_no_path_or_dispatch_declaration():
    definition = ProfilerDefinition(
        kind=ProfilerKind.NSYS,
        domains=frozenset({DomainName.GENERIC}),
    )

    assert definition.server_path == "nsys_profiler/server.py"
    assert definition.prompt_template == "profilers/nsys.j2"


def _expected_resolved(
    requested: ProfilerKind,
    *,
    domain: DomainName,
    backend_profiler_kind: ProfilerKind | None,
    environment_default_profiler_kind: ProfilerKind,
) -> ProfilerKind:
    allowed = allowed_profiler_kinds(domain)
    if requested is not ProfilerKind.AUTO:
        if requested not in allowed:
            raise ValueError
        return requested
    if domain is DomainName.GENERIC and requested is ProfilerKind.AUTO:
        system = platform.system()
        if system == "Darwin":
            return ProfilerKind.MACOS_CPU
        if system == "Linux":
            return ProfilerKind.LINUX_CPU
        return ProfilerKind.NONE
    if domain is DomainName.MICROSERVICES:
        return ProfilerKind.NONE
    if allowed == frozenset({ProfilerKind.NONE}):
        return ProfilerKind.NONE
    if environment_default_profiler_kind is ProfilerKind.TORCH:
        return ProfilerKind.TORCH
    candidate = (
        backend_profiler_kind
        if backend_profiler_kind in ACTIVE_PROFILER_KINDS
        else environment_default_profiler_kind
    )
    if candidate not in allowed:
        raise ValueError
    return candidate


@pytest.mark.parametrize("domain", _DOMAINS)
@pytest.mark.parametrize("requested", _REQUESTED)
@pytest.mark.parametrize("backend_profiler_kind", _BACKEND_KINDS)
@pytest.mark.parametrize("environment_default_profiler_kind", _ENVIRONMENT_DEFAULTS)
def test_profiler_auto_resolution_exhaustive(
    domain,
    requested,
    backend_profiler_kind,
    environment_default_profiler_kind,
):
    kwargs = dict(
        domain=domain,
        backend_profiler_kind=backend_profiler_kind,
        environment_default_profiler_kind=environment_default_profiler_kind,
    )
    try:
        expected = _expected_resolved(requested, **kwargs)
    except ValueError:
        with pytest.raises(ValueError):
            resolve_profiler_kind(requested, **kwargs)
    else:
        assert resolve_profiler_kind(requested, **kwargs) is expected


def test_generic_auto_uses_none_when_host_has_no_native_cpu_profiler(monkeypatch):
    monkeypatch.setattr(platform, "system", lambda: "FreeBSD")

    assert (
        resolve_profiler_kind(
            ProfilerKind.AUTO,
            domain=DomainName.GENERIC,
            backend_profiler_kind=ProfilerKind.LINUX_CPU,
            environment_default_profiler_kind=ProfilerKind.NSYS,
        )
        is ProfilerKind.NONE
    )


@given(
    domain=st.sampled_from(_DOMAINS),
    requested=st.sampled_from(_REQUESTED),
    backend_profiler_kind=st.sampled_from(_BACKEND_KINDS),
    environment_default_profiler_kind=st.sampled_from(_ENVIRONMENT_DEFAULTS),
)
def test_profiler_resolution_invariants(
    domain,
    requested,
    backend_profiler_kind,
    environment_default_profiler_kind,
):
    kwargs = dict(
        domain=domain,
        backend_profiler_kind=backend_profiler_kind,
        environment_default_profiler_kind=environment_default_profiler_kind,
    )
    allowed = allowed_profiler_kinds(domain)
    if requested is not ProfilerKind.AUTO and requested not in allowed:
        with pytest.raises(ValueError):
            resolve_profiler_kind(requested, **kwargs)
        return

    try:
        resolved = resolve_profiler_kind(requested, **kwargs)
    except ValueError:
        assert requested is ProfilerKind.AUTO
        return

    assert resolved is not ProfilerKind.AUTO
    assert resolved in allowed
    if domain is DomainName.GENERIC and requested is ProfilerKind.AUTO:
        system = platform.system()
        if system == "Darwin":
            expected = ProfilerKind.MACOS_CPU
        elif system == "Linux":
            expected = ProfilerKind.LINUX_CPU
        else:
            expected = ProfilerKind.NONE
        assert resolved is expected
    if requested is not ProfilerKind.AUTO:
        assert resolved is requested
    if (
        domain is DomainName.LLM_SERVING
        and requested is ProfilerKind.AUTO
        and environment_default_profiler_kind is ProfilerKind.TORCH
    ):
        assert resolved is ProfilerKind.TORCH


@given(value=st.text(min_size=1, max_size=12).filter(lambda text: text not in _PROFILER_VALUES))
def test_unknown_profiler_names_raise(value):
    with pytest.raises(ValueError, match="Unknown"):
        coerce_profiler_kind(value)


@given(
    backend_profiler_kind=st.text(min_size=1, max_size=12).filter(
        lambda text: text not in _PROFILER_VALUES
    )
)
def test_resolver_rejects_unparsed_backend_profiler_metadata(backend_profiler_kind):
    with pytest.raises(TypeError, match="backend profiler"):
        resolve_profiler_kind(
            ProfilerKind.AUTO,
            domain=DomainName.LLM_SERVING,
            backend_profiler_kind=backend_profiler_kind,
            environment_default_profiler_kind=ProfilerKind.NSYS,
        )


def test_linux_cpu_preflight_fails_when_perf_is_unavailable(monkeypatch):
    from vibesys.linux_cpu_profiler import Capability, DiagnosticCode, LinuxProfilerTool

    monkeypatch.setattr(
        "vibesys.linux_cpu_profiler.detect_capability",
        lambda: Capability(
            LinuxProfilerTool.NONE,
            None,
            None,
            3,
            0,
            (
                DiagnosticCode.PERF_EVENT_PARANOID_RESTRICTIVE,
                DiagnosticCode.PERF_UNAVAILABLE,
            ),
        ),
    )

    result = preflight_profiler_kind(ProfilerKind.LINUX_CPU)

    assert not result.usable
    assert result.diagnostics == ("perf_event_paranoid_restrictive", "perf_unavailable")
    assert "perf_path=missing" in result.details
    assert "perf_unavailable" in result.error_message()


def test_linux_cpu_preflight_accepts_perf_with_nonblocking_symbol_restrictions(monkeypatch):
    from vibesys.linux_cpu_profiler import Capability, DiagnosticCode, LinuxProfilerTool

    monkeypatch.setattr(
        "vibesys.linux_cpu_profiler.detect_capability",
        lambda: Capability(
            LinuxProfilerTool.PERF,
            "/usr/bin/perf",
            "perf version 6.8",
            1,
            1,
            (DiagnosticCode.KERNEL_SYMBOLS_RESTRICTED,),
        ),
    )

    result = preflight_profiler_kind(ProfilerKind.LINUX_CPU)

    assert result.usable
    assert result.diagnostics == ("kernel_symbols_restricted",)
