"""Guards on the ``vs_agent`` public API boundary and its owned fake.

These tests enforce the contract behind the physical ``vs_agent.api`` package:
the public surface stays importable, importing it stays cheap (no agent CLI
backends pulled in), and the library-owned fake in ``vs_agent.api.testing``
keeps the same shape as the real client so integration tests that swap it in
cannot silently drift from production behavior.
"""

from __future__ import annotations

import subprocess
import sys

import vs_agent.api
import vs_agent.api.testing
from vs_agent.api import AgentClient, AgentClientProtocol
from vs_agent.api.testing import FakeAgentClient
from vs_agent.stub_runner import StubAgentClient


def _protocol_members(protocol: type) -> set[str]:
    """Return the member names a concrete class must provide for ``protocol``.

    ``typing.Protocol`` records these in ``__protocol_attrs__`` (3.12+); fall
    back to public names declared on the protocol if that attribute ever moves.
    """
    members = getattr(protocol, "__protocol_attrs__", None)
    if members:
        return set(members)
    found: set[str] = set()
    for klass in protocol.__mro__:
        if klass is object:
            continue
        found.update(vars(klass))
        found.update(getattr(klass, "__annotations__", {}))
    return {name for name in found if not name.startswith("_")}


def test_public_names_all_resolve() -> None:
    """Every name in ``__all__`` (eager and lazy) is importable."""
    for name in vs_agent.api.__all__:
        assert getattr(vs_agent.api, name) is not None, name
    for name in vs_agent.api.testing.__all__:
        assert getattr(vs_agent.api.testing, name) is not None, name


def test_importing_api_does_not_load_agent_backends() -> None:
    """``import vs_agent.api`` must not pull in agentshim/omnigent.

    Checked in a fresh interpreter because other tests in this session may have
    already imported those backends, polluting this process's ``sys.modules``.
    """
    code = (
        "import sys, vs_agent.api\n"
        "leaked = sorted(m for m in sys.modules "
        "if m == 'agentshim' or m == 'omnigent' "
        "or m.startswith(('agentshim.', 'omnigent.')))\n"
        "assert not leaked, leaked\n"
        "print('clean')\n"
    )
    result = subprocess.run(  # noqa: S603  # trusted: our interpreter, literal code
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "clean"


def test_stub_fake_conforms_to_client_protocol() -> None:
    """The owned fake exposes every member of ``AgentClientProtocol``.

    This is the per-library "keep the fake correct" guard: if the real client
    contract grows a member, the stub must grow it too or this fails.
    """
    required = _protocol_members(AgentClientProtocol)
    assert required, "expected a non-empty protocol member set"
    for member in required:
        assert hasattr(StubAgentClient, member), f"stub missing {member!r}"
        assert hasattr(FakeAgentClient, member), f"fake missing {member!r}"
        assert hasattr(AgentClient, member), f"real client missing {member!r}"


def test_stub_fake_is_constructible_and_usable() -> None:
    """The fake constructs with no arguments and answers basic queries."""
    fake = StubAgentClient()
    assert fake.backend_name == "stub"
    assert fake.driver_name == "stub"
    assert fake.capabilities is not None
