"""Provider sign-in state and write-only API keys in the checkout's `.env`.

The home server never loads `.env` into its own environment (no `load_config`,
no `load_dotenv`): it reads the file with `dotenv_values`, so a key saved here
reaches the next launched run instead of being shadowed by a stale copy in
this process.
"""

from __future__ import annotations

import io
import unicodedata
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from entrypoints.web_home.context import atomic_write, parse_body
from entrypoints.web_home.contract import (
    ApiError,
    AuthStatus,
    ErrorCode,
    KeyVar,
    KeyWrite,
    KeyWriteResult,
    ProviderAuth,
)
from vs_agent.api import SHIPPED_PROVIDERS, credential_path, provider_profile

if TYPE_CHECKING:
    from collections.abc import Mapping

    from agentshim import ProviderProfile

    from entrypoints.web_home.context import HomeConfig, Request

# Only secrets are writable: the profile's other auth variables (base URLs, headers) are not keys.
_KEY_SUFFIXES = ("_API_KEY", "_AUTH_TOKEN")
_LOGIN_COMMANDS = {"codex": "codex login", "opencode": "opencode auth login"}
# Claude Code on macOS keeps its login in the keychain instead of `.credentials.json`.
_KEYCHAIN_SERVICES = {"claude": "Claude Code-credentials"}


def key_variables(profile: ProviderProfile) -> tuple[str, ...]:
    """Return the allowlisted key variables of one provider, in profile order."""
    return tuple(name for name in profile.auth_env_vars if name.endswith(_KEY_SUFFIXES))


def _stored(path: Path) -> dict[str, str | None]:
    return dict(dotenv_values(path)) if path.is_file() else {}


def _key_var(name: str, environ: Mapping[str, str], stored: Mapping[str, str | None]) -> KeyVar:
    # load_dotenv(override=False) skips any name already in the environment, even an empty one,
    # so membership, not truthiness, decides what a launched run sees.
    inherited = name in environ
    if inherited:
        source = "env" if environ[name] else "missing"
    else:
        source = "dotenv" if stored.get(name) else "missing"
    return KeyVar(name=name, source=source, shadowed=inherited and name in stored)


def _cli_session(
    profile: ProviderProfile, config: HomeConfig
) -> Literal["present", "absent", "unknown"]:
    """Report a CLI login by presence only (unverified), never by reading a secret."""
    environ = config.environ
    home = environ.get("HOME")
    path = credential_path(profile, home=Path(home), env=environ) if home else None
    if path is not None and path.is_file():
        return "present"
    service = _KEYCHAIN_SERVICES.get(profile.name)
    if service is None:
        return "absent"
    found = config.keychain(service)
    return "unknown" if found is None else "present" if found else "absent"


def _provider_auth(name: str, config: HomeConfig, stored: Mapping[str, str | None]) -> ProviderAuth:
    profile = provider_profile(name)
    keys = [_key_var(variable, config.environ, stored) for variable in key_variables(profile)]
    cli_session = _cli_session(profile, config)
    if any(key.source != "missing" for key in keys):
        status = "key"
    elif cli_session == "present":
        status = "cli_session"
    else:
        status = "missing"
    return ProviderAuth(
        provider=name,
        display_name=profile.display_name,
        status=status,
        keys=keys,
        cli_session=cli_session,
        login_command=_LOGIN_COMMANDS.get(name, profile.binary),
    )


def auth_status(request: Request) -> AuthStatus:
    """``GET /api/auth``: where each provider's credentials come from; values never leave."""
    config = request.config
    stored = _stored(config.dotenv_path)
    return AuthStatus(
        providers=[_provider_auth(name, config, stored) for name in SHIPPED_PROVIDERS],
        dotenv_path=str(config.dotenv_path),
    )


def _check_value(value: str) -> None:
    if not value.strip():
        message = "the key is empty"
        raise ApiError(ErrorCode.INVALID_KEY, message)
    if any(unicodedata.category(character) == "Cc" for character in value):
        message = "the key contains a control character"
        raise ApiError(ErrorCode.INVALID_KEY, message)
    if any(character in value for character in "'\"\\$"):
        message = "the key contains a quote, backslash, or `$`"
        raise ApiError(ErrorCode.INVALID_KEY, message)


def store_key(path: Path, name: str, value: str) -> None:
    """Set ``name='value'`` in the `.env` at *path*, keeping every other byte of the file.

    The caller has rejected quotes, backslashes, ``$``, and control characters,
    so python-dotenv neither unescapes nor interpolates the value. Bytes are
    decoded without newline translation, so CRLF files keep their line endings.
    """
    text = path.read_bytes().decode("utf-8") if path.is_file() else ""
    line = f"{name}='{value}'\n"
    kept: list[str] = []
    for binding in parse_stream(io.StringIO(text)):
        if binding.key != name:
            kept.append(binding.original.string)
        elif line not in kept:
            kept.append(line)
    if line not in kept:
        if kept and not kept[-1].endswith("\n"):
            kept.append("\n")
        kept.append(line)
    atomic_write(path, "".join(kept).encode("utf-8"), mode=0o600)


def write_key(request: Request) -> KeyWriteResult:
    """``PUT /api/auth/{provider}``: store one allowlisted key; the value is never returned."""
    provider = request.params[0]
    if provider not in SHIPPED_PROVIDERS:
        message = f"unknown provider {provider!r}"
        raise ApiError(ErrorCode.UNKNOWN_PROVIDER, message)
    body = parse_body(request, KeyWrite)
    allowed = key_variables(provider_profile(provider))
    if body.name not in allowed:
        expected = ", ".join(allowed) or "none; sign in with the provider's CLI"
        message = f"{body.name!r} is not a key variable for {provider} (expected: {expected})"
        raise ApiError(ErrorCode.INVALID_KEY, message)
    value = body.value.get_secret_value()
    _check_value(value)
    config = request.config
    with config.write_lock:
        store_key(config.dotenv_path, body.name, value)
    return KeyWriteResult(
        provider=provider,
        name=body.name,
        status="unverified",
        shadowed_by_env=body.name in config.environ,
    )
