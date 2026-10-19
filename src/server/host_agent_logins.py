"""Whether each agent harness is signed in on a machine, as helper messages.

``agent_logins.list`` (feature ``agent_logins``) answers, per vetted harness on the machine the
helper runs on, whether its CLI is installed, whether it is signed in and how (an account, or an
API key held in a named environment variable or file, never the secret), and which variables
would sign it in. ``agent_logins.start`` asks the helper to start the harness's own login there.
Every fact comes from agentshim through ``vs_agent``; this module only names them on the wire.

Light: Pydantic, the standard library and ``server.host_envelope``. ``server.host_api`` imports
the messages into the helper's request and reply unions.

Public surface: ``AccountLogin``, ``ApiKeyLogin``, ``LoginHow``, ``LoginState``, ``AgentLogin``,
``LoginStarted``, ``LoginUnavailableReason``, ``LoginUnavailable``, ``LoginStartResult`` and the
two requests and replies.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from server.host_envelope import HelperReplyBase, HelperRequestBase

_Line = Annotated[str, StringConstraints(max_length=300, pattern=r"^[^\n\r]*$")]
_Name = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class LoginState(StrEnum):
    """Whether a harness is signed in, as far as its own tooling can say."""

    SIGNED_IN = "signed_in"
    SIGNED_OUT = "signed_out"
    UNKNOWN = "unknown"
    """Not installed, or the CLI offers no cheap check: not a problem to report as one."""


class AccountLogin(BaseModel):
    """Signed in with a person's account (a subscription login)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["account"] = "account"


class ApiKeyLogin(BaseModel):
    """Signed in with an API key; where it is held is named, the key never."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["api_key"] = "api_key"
    variable: _Name | None = None
    """The environment variable that holds the key."""
    file: _Name | None = None
    """The home-relative file that holds the key."""

    @model_validator(mode="after")
    def _one_place(self) -> ApiKeyLogin:
        if self.variable is not None and self.file is not None:
            message = "an API key is held in a variable or a file, not both"
            raise ValueError(message)
        return self


LoginHow = Annotated[AccountLogin | ApiKeyLogin, Field(discriminator="kind")]
"""How a harness is signed in; ``kind`` discriminates."""


class AgentLogin(BaseModel):
    """One harness's login on the machine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: _Name
    """The harness, as ``harnesses.list`` names its provider."""
    provider_name: _Name
    """The harness as people call it ("Claude Code")."""
    installed: bool
    state: LoginState
    how: LoginHow | None = None
    """How it is signed in; present only when signed in, and absent when the CLI did not say."""
    api_key_variables: tuple[_Name, ...] = ()
    """The environment variables that would sign it in with an API key, in its precedence order."""
    detail: _Line = ""
    """The CLI's own words about its login, one line."""
    can_start_login: bool
    """Whether ``agent_logins.start`` can start this harness's own login on the machine."""

    @model_validator(mode="after")
    def _facts_agree(self) -> AgentLogin:
        if not self.installed and self.state is not LoginState.UNKNOWN:
            message = "a harness that is not installed has an unknown login"
            raise ValueError(message)
        if self.how is not None and self.state is not LoginState.SIGNED_IN:
            message = "how is named only for a signed-in harness"
            raise ValueError(message)
        return self


class LoginStarted(BaseModel):
    """The harness's own login was started on the machine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal["started"] = "started"
    instruction: _Line | None = None
    """What the login asks the person to do (a URL to open, a code), when it said."""


class LoginUnavailableReason(StrEnum):
    """Why a login could not be started."""

    NOT_INSTALLED = "not_installed"
    NO_LOGIN_COMMAND = "no_login_command"
    """agentshim names no command that starts this harness's login."""


class LoginUnavailable(BaseModel):
    """The login was not started, and why."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal["unavailable"] = "unavailable"
    reason: LoginUnavailableReason


LoginStartResult = Annotated[LoginStarted | LoginUnavailable, Field(discriminator="outcome")]
"""What asking for a login did; ``outcome`` discriminates."""


class AgentLoginsListRequest(HelperRequestBase):
    """Each vetted harness's login on this machine."""

    type: Literal["agent_logins.list"] = "agent_logins.list"


class AgentLoginsStartRequest(HelperRequestBase):
    """Start the harness's own login on this machine."""

    type: Literal["agent_logins.start"] = "agent_logins.start"
    provider: _Name
    """A provider ``agent_logins.list`` listed; another is ``unknown_provider``."""


class AgentLoginsListReply(HelperReplyBase):
    """The logins, in the order ``harnesses.list`` offers the providers."""

    type: Literal["agent_logins.list"] = "agent_logins.list"
    logins: tuple[AgentLogin, ...]


class AgentLoginsStartReply(HelperReplyBase):
    """What starting the login did."""

    type: Literal["agent_logins.start"] = "agent_logins.start"
    result: LoginStartResult


__all__ = [
    "AccountLogin",
    "AgentLogin",
    "AgentLoginsListReply",
    "AgentLoginsListRequest",
    "AgentLoginsStartReply",
    "AgentLoginsStartRequest",
    "ApiKeyLogin",
    "LoginHow",
    "LoginStartResult",
    "LoginStarted",
    "LoginState",
    "LoginUnavailable",
    "LoginUnavailableReason",
]
