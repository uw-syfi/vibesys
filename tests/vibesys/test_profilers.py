from __future__ import annotations

import platform
from typing import TypedDict, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from vibesys.constants import DomainName
from vibesys.linux_cpu_profiler import (
    Capability,
    DiagnosticCode,
    LinuxProfilerTool,
)
from vibesys.macos_cpu_profiler import Capability as MacOSCapability
from vibesys.macos_cpu_profiler import MacOSProfilerTool
from vibesys.profilers import (
    ACTIVE_PROFILER_KINDS,
    PROFILER_DEFINITIONS,
    ProfilerDefinition,
    ProfilerKind,
    allowed_profiler_kinds,
    coerce_profiler_kind,
    preflight_profiler_kind,
    profiler_definition,
    require_domain_name,
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


class _ResolutionInputs(TypedDict):
    domain: DomainName
    backend_profiler_kind: ProfilerKind | None
    environment_default_profiler_kind: ProfilerKind


def test_profiler_definitions_derive_uniform_packaging_names() -> None:
    assert frozenset(PROFILER_DEFINITIONS) == ACTIVE_PROFILER_KINDS
    for kind, definition in PROFILER_DEFINITIONS.items():
        assert definition.support_name == f"{kind.value}_profiler"
        assert definition.server_path == f"{kind.value}_profiler/server.py"
        assert definition.prompt_template == f"profilers/{kind.value}.j2"
        assert definition.mcp_name == f"vibesys-{kind.value.replace('_', '-')}-profiler"


def test_profiler_definition_needs_no_path_or_dispatch_declaration() -> None:
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
        return {
            "Darwin": ProfilerKind.MACOS_CPU,
            "Linux": ProfilerKind.LINUX_CPU,
        }.get(system, ProfilerKind.NONE)
    if domain is DomainName.MICROSERVICES:
        return ProfilerKind.NONE
    if allowed == frozenset({ProfilerKind.NONE}):
        return ProfilerKind.NONE
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
    domain: DomainName,
    requested: ProfilerKind,
    backend_profiler_kind: ProfilerKind | None,
    environment_default_profiler_kind: ProfilerKind,
) -> None:
    kwargs: _ResolutionInputs = {
        "domain": domain,
        "backend_profiler_kind": backend_profiler_kind,
        "environment_default_profiler_kind": environment_default_profiler_kind,
    }
    try:
        expected = _expected_resolved(requested, **kwargs)
    except ValueError:
        with pytest.raises(ValueError, match="not supported"):
            resolve_profiler_kind(requested, **kwargs)
    else:
        assert resolve_profiler_kind(requested, **kwargs) is expected


def test_generic_auto_uses_none_when_host_has_no_native_cpu_profiler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


def test_generic_auto_respects_environment_profiler_capabilities(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    assert (
        resolve_profiler_kind(
            ProfilerKind.AUTO,
            domain=DomainName.GENERIC,
            backend_profiler_kind=ProfilerKind.LINUX_CPU,
            environment_default_profiler_kind=ProfilerKind.TORCH,
            environment_supported_profiler_kinds=frozenset(
                {ProfilerKind.AUTO, ProfilerKind.TORCH, ProfilerKind.NONE}
            ),
        )
        is ProfilerKind.NONE
    )


def test_explicit_profiler_respects_environment_capabilities() -> None:
    with pytest.raises(ValueError, match="selected run environment"):
        resolve_profiler_kind(
            ProfilerKind.NSYS,
            domain=DomainName.LLM_SERVING,
            backend_profiler_kind=ProfilerKind.NSYS,
            environment_default_profiler_kind=ProfilerKind.TORCH,
            environment_supported_profiler_kinds=frozenset(
                {ProfilerKind.AUTO, ProfilerKind.TORCH, ProfilerKind.NONE}
            ),
        )


def test_auto_profiler_falls_back_by_environment_capability_not_provider_name() -> None:
    assert (
        resolve_profiler_kind(
            ProfilerKind.AUTO,
            domain=DomainName.LLM_SERVING,
            backend_profiler_kind=ProfilerKind.NSYS,
            environment_default_profiler_kind=ProfilerKind.TORCH,
            environment_supported_profiler_kinds=frozenset(
                {ProfilerKind.AUTO, ProfilerKind.TORCH, ProfilerKind.NONE}
            ),
        )
        is ProfilerKind.TORCH
    )


@given(
    domain=st.sampled_from(_DOMAINS),
    requested=st.sampled_from(_REQUESTED),
    backend_profiler_kind=st.sampled_from(_BACKEND_KINDS),
    environment_default_profiler_kind=st.sampled_from(_ENVIRONMENT_DEFAULTS),
)
def test_profiler_resolution_invariants(
    domain: DomainName,
    requested: ProfilerKind,
    backend_profiler_kind: ProfilerKind | None,
    environment_default_profiler_kind: ProfilerKind,
) -> None:
    kwargs: _ResolutionInputs = {
        "domain": domain,
        "backend_profiler_kind": backend_profiler_kind,
        "environment_default_profiler_kind": environment_default_profiler_kind,
    }
    allowed = allowed_profiler_kinds(domain)
    if requested is not ProfilerKind.AUTO and requested not in allowed:
        with pytest.raises(ValueError, match="not supported"):
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


@given(value=st.text(min_size=1, max_size=12).filter(lambda text: text not in _PROFILER_VALUES))
def test_unknown_profiler_names_raise(value: str) -> None:
    with pytest.raises(ValueError, match="Unknown"):
        coerce_profiler_kind(value)


@given(
    backend_profiler_kind=st.text(min_size=1, max_size=12).filter(
        lambda text: text not in _PROFILER_VALUES
    )
)
def test_resolver_rejects_unparsed_backend_profiler_metadata(backend_profiler_kind: str) -> None:
    with pytest.raises(TypeError, match="backend profiler"):
        resolve_profiler_kind(
            ProfilerKind.AUTO,
            domain=DomainName.LLM_SERVING,
            backend_profiler_kind=cast("ProfilerKind", backend_profiler_kind),
            environment_default_profiler_kind=ProfilerKind.NSYS,
        )


def test_linux_cpu_preflight_fails_when_perf_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

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


def test_linux_cpu_preflight_accepts_perf_with_nonblocking_symbol_restrictions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:

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


def test_headroom_profiler_domains_and_preflight() -> None:
    definition = PROFILER_DEFINITIONS[ProfilerKind.HEADROOM]

    assert definition.domains == frozenset({DomainName.LLM_SERVING})
    assert not definition.requires_domain_torch_support
    assert ProfilerKind.HEADROOM in allowed_profiler_kinds(DomainName.LLM_SERVING)
    assert ProfilerKind.HEADROOM not in allowed_profiler_kinds(DomainName.GENERIC)
    assert ProfilerKind.HEADROOM not in allowed_profiler_kinds(DomainName.MICROSERVICES)
    # Capture is target-owned; the analysis side needs no host tooling.
    assert preflight_profiler_kind(ProfilerKind.HEADROOM).usable


def test_rocprof_profiler_domains_and_preflight() -> None:
    definition = PROFILER_DEFINITIONS[ProfilerKind.ROCPROF]

    assert definition.domains == frozenset({DomainName.LLM_SERVING})
    assert not definition.requires_domain_torch_support
    assert ProfilerKind.ROCPROF in allowed_profiler_kinds(DomainName.LLM_SERVING)
    assert ProfilerKind.ROCPROF not in allowed_profiler_kinds(DomainName.GENERIC)
    assert ProfilerKind.ROCPROF not in allowed_profiler_kinds(DomainName.MICROSERVICES)
    # rocprofv3/rocprof-compute normally run inside the ROCm container, like
    # nsys on CUDA; no host command-availability check, to avoid a false
    # negative on an editor host that never runs the profiler itself.
    assert preflight_profiler_kind(ProfilerKind.ROCPROF).usable
    # The rocprof MCP server also exposes the torch analyzer's tools, so
    # torch_profiler/ is staged alongside rocprof_profiler/.
    assert definition.extra_support_kinds == frozenset({ProfilerKind.TORCH})


def test_extra_support_kinds_default_empty_and_are_runnable_kinds() -> None:
    for kind, definition in PROFILER_DEFINITIONS.items():
        if kind is ProfilerKind.ROCPROF:
            continue
        assert definition.extra_support_kinds == frozenset()
    for definition in PROFILER_DEFINITIONS.values():
        for extra_kind in definition.extra_support_kinds:
            assert extra_kind in PROFILER_DEFINITIONS


@pytest.mark.parametrize("kind", [ProfilerKind.AUTO, ProfilerKind.NONE])
def test_profiler_definition_rejects_non_runnable_kinds(kind: ProfilerKind) -> None:
    with pytest.raises(ValueError, match=f"Profiler {kind.value!r} is not runnable"):
        profiler_definition(kind)


def test_profiler_definition_returns_declaration_for_runnable_kind() -> None:
    assert profiler_definition(ProfilerKind.NSYS) is PROFILER_DEFINITIONS[ProfilerKind.NSYS]


def test_require_domain_name_rejects_raw_strings() -> None:
    with pytest.raises(TypeError, match="domain must be a DomainName, got str"):
        require_domain_name("generic")
    assert require_domain_name(DomainName.GENERIC) is DomainName.GENERIC


def test_generic_auto_falls_back_to_supported_environment_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    assert (
        resolve_profiler_kind(
            ProfilerKind.AUTO,
            domain=DomainName.GENERIC,
            backend_profiler_kind=None,
            environment_default_profiler_kind=ProfilerKind.MACOS_CPU,
            environment_supported_profiler_kinds=frozenset(
                {ProfilerKind.MACOS_CPU, ProfilerKind.NSYS}
            ),
        )
        is ProfilerKind.MACOS_CPU
    )


def test_generic_auto_errors_when_environment_supports_no_generic_profiler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(platform, "system", lambda: "Linux")

    with pytest.raises(
        ValueError, match=r"No profiler supported by both.*environment allows: nsys"
    ):
        resolve_profiler_kind(
            ProfilerKind.AUTO,
            domain=DomainName.GENERIC,
            backend_profiler_kind=None,
            environment_default_profiler_kind=ProfilerKind.NSYS,
            environment_supported_profiler_kinds=frozenset({ProfilerKind.NSYS}),
        )


def test_auto_rejects_environment_default_the_environment_cannot_run() -> None:
    with pytest.raises(
        ValueError,
        match=r"Resolved profiler 'torch' is not supported by the selected run environment; "
        r"allowed: none",
    ):
        resolve_profiler_kind(
            ProfilerKind.AUTO,
            domain=DomainName.LLM_SERVING,
            backend_profiler_kind=ProfilerKind.NSYS,
            environment_default_profiler_kind=ProfilerKind.TORCH,
            environment_supported_profiler_kinds=frozenset({ProfilerKind.NONE}),
        )


@pytest.mark.parametrize(
    ("tool", "sample_path"),
    [(MacOSProfilerTool.SAMPLE, "/usr/bin/sample"), (MacOSProfilerTool.NONE, None)],
)
def test_macos_cpu_preflight_reports_tool_availability(
    monkeypatch: pytest.MonkeyPatch,
    tool: MacOSProfilerTool,
    sample_path: str | None,
) -> None:
    monkeypatch.setattr(
        "vibesys.macos_cpu_profiler.detect_capability",
        lambda: MacOSCapability(tool, None, None, sample_path, None, ()),
    )

    result = preflight_profiler_kind(ProfilerKind.MACOS_CPU)

    assert result.usable is (tool is MacOSProfilerTool.SAMPLE)
    assert result.diagnostics == ()
    assert result.details == (
        "xcode_path=missing",
        "xctrace_path=missing",
        f"sample_path={sample_path or 'missing'}",
    )
