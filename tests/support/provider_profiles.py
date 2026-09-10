"""Provider profiles for tests, without depending on what agentshim ships.

VibeSys derives its Docker and host-resource tables from
``ProviderProfile``. A test of that derivation should fail when the derivation
changes, not when a library release edits one CLI's install recipe, so tests
build the profiles they need here and install them through
``vibesys.agents.provider_profiles``. Tests whose subject *is* a real
provider's declared behaviour read the shipped profile instead.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from agentshim import McpMechanism, OutputSchemaStyle, ProviderProfile, SchemaDialect

from vibesys.agents import provider_profiles

if TYPE_CHECKING:
    from collections.abc import Mapping

    import pytest


def profile(  # noqa: PLR0913
    name: str,
    *,
    supports_resume: bool = True,
    supports_reasoning_effort: bool = True,
    mcp: McpMechanism = McpMechanism.CONFIG_FILE,
    output_schema: OutputSchemaStyle = OutputSchemaStyle.INLINE_JSON,
    schema_dialect: SchemaDialect | None = SchemaDialect.OPEN,
    state_dirs: tuple[str, ...] = (),
    darwin_state_dirs: tuple[str, ...] = (),
    auth_env_vars: tuple[str, ...] = (),
    skill_dirs: tuple[str, ...] = (),
    container_install: tuple[str, ...] = (),
    container_env: Mapping[str, str] | None = None,
    state_root_env: str | None = None,
    auth_files: tuple[str, ...] = (),
    mcp_config_file: str | None = None,
) -> ProviderProfile:
    """Build a profile for *name*, defaulting every field a test ignores.

    Only the fields a test asserts on need to be passed. The defaults are
    deliberately uninteresting: a test that depends on one of them is testing
    this helper, not VibeSys.
    """
    return ProviderProfile(
        name=name,
        display_name=name.title(),
        binary=name,
        supports_resume=supports_resume,
        supports_reasoning_effort=supports_reasoning_effort,
        mcp=mcp,
        output_schema=output_schema,
        schema_dialect=schema_dialect,
        state_dirs=state_dirs,
        darwin_state_dirs=darwin_state_dirs,
        auth_env_vars=auth_env_vars,
        skill_dirs=skill_dirs,
        container_install=container_install,
        container_env=container_env if container_env is not None else {},
        state_root_env=state_root_env,
        auth_files=auth_files,
        mcp_config_file=mcp_config_file,
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
            raise ValueError(f"unknown provider {provider!r}") from None  # noqa: TRY003

    monkeypatch.setattr(provider_profiles, "provider_profile", lookup)
