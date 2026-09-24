"""Provider profiles for tests, without depending on what agentshim ships.

VibeSys derives its Docker and host-resource tables from
``ProviderProfile``. A test of that derivation should fail when the derivation
changes, not when a library release edits one CLI's install recipe, so tests
build the profiles they need here and install them through
``vs_agent.provider_profiles``. Tests whose subject *is* a real
provider's declared behaviour read the shipped profile instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict, Unpack

from agentshim import McpMechanism, OutputSchemaStyle, ProviderProfile, SchemaDialect

from vs_agent import provider_profiles

if TYPE_CHECKING:
    from collections.abc import Mapping

    import pytest


class _ProfileOptions(TypedDict, total=False):
    supports_resume: bool
    supports_reasoning_effort: bool
    mcp: McpMechanism
    output_schema: OutputSchemaStyle
    schema_dialect: SchemaDialect | None
    state_dirs: tuple[str, ...]
    darwin_state_dirs: tuple[str, ...]
    auth_env_vars: tuple[str, ...]
    skill_dirs: tuple[str, ...]
    container_install: tuple[str, ...]
    container_env: Mapping[str, str] | None
    state_root_env: str | None
    auth_files: tuple[str, ...]
    mcp_config_file: str | None


def profile(name: str, **options: Unpack[_ProfileOptions]) -> ProviderProfile:
    """Build a profile for *name*, defaulting every field a test ignores.

    Only the fields a test asserts on need to be passed. The defaults are
    deliberately uninteresting: a test that depends on one of them is testing
    this helper, not VibeSys.
    """
    container_env = options.get("container_env")
    return ProviderProfile(
        name=name,
        display_name=name.title(),
        binary=name,
        supports_resume=options.get("supports_resume", True),
        supports_reasoning_effort=options.get("supports_reasoning_effort", True),
        mcp=options.get("mcp", McpMechanism.CONFIG_FILE),
        output_schema=options.get("output_schema", OutputSchemaStyle.INLINE_JSON),
        schema_dialect=options.get("schema_dialect", SchemaDialect.OPEN),
        state_dirs=options.get("state_dirs", ()),
        darwin_state_dirs=options.get("darwin_state_dirs", ()),
        auth_env_vars=options.get("auth_env_vars", ()),
        skill_dirs=options.get("skill_dirs", ()),
        container_install=options.get("container_install", ()),
        container_env=container_env if container_env is not None else {},
        state_root_env=options.get("state_root_env"),
        auth_files=options.get("auth_files", ()),
        mcp_config_file=options.get("mcp_config_file"),
    )


def install(
    monkeypatch: pytest.MonkeyPatch,
    profiles: dict[str, ProviderProfile],
) -> None:
    """Make ``provider_profile`` answer from *profiles*, and nothing else."""

    def lookup(provider: str) -> ProviderProfile:
        try:
            return profiles[provider]
        except KeyError:
            _failure_message = f"unknown provider {provider!r}"
            raise ValueError(_failure_message) from None

    monkeypatch.setattr(provider_profiles, "provider_profile", lookup)
