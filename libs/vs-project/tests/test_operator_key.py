"""The operator key: its file, its hello, and the dialer that presents it."""

from __future__ import annotations

import os
import stat
from itertools import count
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from vs_project.api import (
    CONTROL_HELLO_MAX_BYTES,
    OperatorDialer,
    OperatorKey,
    OperatorKeyError,
    OperatorSocketOutsideRootError,
    create_operator_key,
    operator_key_path,
    read_operator_key,
)
from vs_sim.api.testing import HANG_GUARD_S, SimNetwork, SimThreads

if TYPE_CHECKING:
    from vs_sim.api import Connection, Listener

_UID = os.getuid()
_FRESH = count()
_SECRETS = st.text(alphabet="0123456789abcdef", min_size=64, max_size=64)


def _environ(tmp_path: Path) -> dict[str, str]:
    runtime = tmp_path / f"runtime-{next(_FRESH)}"
    runtime.mkdir(mode=0o700)
    return {"XDG_RUNTIME_DIR": str(runtime)}


def test_the_first_load_creates_a_private_key_and_every_later_load_returns_it(
    tmp_path: Path,
) -> None:
    environ = _environ(tmp_path)

    first = create_operator_key(environ, _UID)
    again = create_operator_key(environ, _UID)

    path = operator_key_path(environ, _UID)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert again.admits(first.hello())
    assert sorted(entry.name for entry in path.parent.iterdir()) == [path.name]


def test_the_key_lives_in_the_runtime_root_and_never_prints(tmp_path: Path) -> None:
    environ = _environ(tmp_path)
    key = create_operator_key(environ, _UID)

    assert operator_key_path(environ, _UID) == Path(environ["XDG_RUNTIME_DIR"]) / "vibesys" / (
        "control.key"
    )
    secret = operator_key_path(environ, _UID).read_text().strip()
    assert secret not in repr(key)
    assert secret not in str(key)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(mode=st.integers(0o000, 0o777))
def test_a_key_file_others_can_reach_or_the_owner_cannot_read_is_refused(
    tmp_path: Path, mode: int
) -> None:
    environ = _environ(tmp_path)
    create_operator_key(environ, _UID)
    path = operator_key_path(environ, _UID)
    path.chmod(mode)
    try:
        if mode & 0o077 or not mode & 0o400:
            with pytest.raises(OperatorKeyError):
                create_operator_key(environ, _UID)
        else:
            create_operator_key(environ, _UID)
    finally:
        path.chmod(0o600)


@pytest.mark.parametrize("contents", [b"", b"short\n", b"g" * 64 + b"\n", b"a" * 65 + b"\n"])
def test_a_key_file_that_does_not_hold_one_key_is_refused(tmp_path: Path, contents: bytes) -> None:
    environ = _environ(tmp_path)
    path = operator_key_path(environ, _UID)
    path.parent.mkdir(mode=0o700)
    path.write_bytes(contents)
    path.chmod(0o600)

    with pytest.raises(OperatorKeyError, match="does not hold one key"):
        create_operator_key(environ, _UID)


def test_a_symlinked_key_file_is_refused(tmp_path: Path) -> None:
    environ = _environ(tmp_path)
    elsewhere = tmp_path / "planted"
    elsewhere.write_text("a" * 64 + "\n")
    elsewhere.chmod(0o600)
    path = operator_key_path(environ, _UID)
    path.parent.mkdir(mode=0o700)
    path.symlink_to(elsewhere)

    with pytest.raises(OperatorKeyError, match="cannot be opened"):
        create_operator_key(environ, _UID)


@given(secret=_SECRETS, line=st.binary(max_size=2 * CONTROL_HELLO_MAX_BYTES))
def test_a_key_admits_its_own_hello_and_nothing_else(secret: str, line: bytes) -> None:
    key = OperatorKey(secret)

    assert key.admits(key.hello())
    assert len(key.hello()) <= CONTROL_HELLO_MAX_BYTES
    assert key.admits(line) == (line == key.hello())


@given(secret=_SECRETS, other=_SECRETS)
def test_a_key_does_not_admit_another_keys_hello(secret: str, other: str) -> None:
    assert OperatorKey(secret).admits(OperatorKey(other).hello()) == (secret == other)


@pytest.mark.parametrize("secret", ["", "A" * 64, "a" * 63, "a" * 65, "z" * 64])
def test_a_key_is_64_lowercase_hex_digits(secret: str) -> None:
    with pytest.raises(ValueError, match="lowercase hex"):
        OperatorKey(secret)


def test_an_operator_dialer_presents_the_key_before_anything_else() -> None:
    threads = SimThreads(schedule_seed=0)
    key = OperatorKey.generate()

    def main() -> bytes:
        network = SimNetwork(threads)
        listener = network.listen("/run/control.sock")
        client = OperatorDialer(network, key, root=Path("/run")).connect(
            "/run/control.sock", HANG_GUARD_S
        )
        client.send(b"request\n", HANG_GUARD_S)
        client.close()
        server = listener.accept(HANG_GUARD_S)
        received = b""
        while chunk := server.recv(4096, HANG_GUARD_S):
            received += chunk
        return received

    assert threads.run(main) == key.hello() + b"request\n"


def test_a_client_reads_the_key_but_never_creates_it(tmp_path: Path) -> None:
    environ = _environ(tmp_path)

    with pytest.raises(OperatorKeyError, match="XDG_RUNTIME_DIR"):
        read_operator_key(environ, _UID)

    assert not operator_key_path(environ, _UID).exists()
    created = create_operator_key(environ, _UID)
    assert read_operator_key(environ, _UID).admits(created.hello())


class _RecordingNetwork:
    """A ``Network`` that records the addresses it is asked to connect to."""

    def __init__(self, threads: SimThreads) -> None:
        self.inner = SimNetwork(threads)
        self.dialed: list[str] = []

    def listen(self, address: str) -> Listener:
        return self.inner.listen(address)

    def connect(self, address: str, timeout: float | None = None) -> Connection:
        self.dialed.append(address)
        return self.inner.connect(address, timeout)


_NAMES = st.text(alphabet="abcdefgh", min_size=1, max_size=4)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=40)
@given(inside=st.lists(_NAMES, max_size=3), outside=st.lists(_NAMES, min_size=1, max_size=3))
def test_a_dialer_presents_the_key_only_to_a_socket_inside_its_root(
    tmp_path: Path, inside: list[str], outside: list[str]
) -> None:
    """A socket anywhere under the root is dialed at its resolved path; elsewhere, nothing."""
    base = tmp_path / f"case-{next(_FRESH)}"
    root = base / "root"
    within = root.joinpath(*inside)
    beside = base.joinpath("elsewhere", *outside)
    for directory in (within, beside):
        directory.mkdir(parents=True)
    threads = SimThreads(schedule_seed=0)
    network = _RecordingNetwork(threads)
    dialer = OperatorDialer(network, OperatorKey.generate(), root=root)

    def main() -> None:
        network.listen(str(within / "control.sock"))
        dialer.connect(str(within / "control.sock"), HANG_GUARD_S).close()
        with pytest.raises(OperatorSocketOutsideRootError):
            dialer.connect(str(beside / "control.sock"), HANG_GUARD_S)
        # The root's own parent and a sibling sharing its name as a prefix are outside too.
        with pytest.raises(OperatorSocketOutsideRootError):
            dialer.connect(str(base / "control.sock"), HANG_GUARD_S)
        sibling = base / "root-other"
        sibling.mkdir(exist_ok=True)
        with pytest.raises(OperatorSocketOutsideRootError):
            dialer.connect(str(sibling / "control.sock"), HANG_GUARD_S)

    threads.run(main)

    assert network.dialed == [str(within.resolve() / "control.sock")]


def test_a_dialer_follows_a_symlinked_directory_only_into_its_root(tmp_path: Path) -> None:
    """The check and the connect use the resolved directory, so a link cannot redirect it."""
    root = tmp_path / "root"
    (root / "session").mkdir(parents=True)
    agent_writable = tmp_path / "tmp"
    agent_writable.mkdir()
    (agent_writable / "into-root").symlink_to(root / "session")
    (agent_writable / "planted").mkdir()
    (agent_writable / "out-of-root").symlink_to(agent_writable / "planted")
    threads = SimThreads(schedule_seed=0)
    network = _RecordingNetwork(threads)
    dialer = OperatorDialer(network, OperatorKey.generate(), root=root)

    def main() -> None:
        network.listen(str(root / "session" / "control.sock"))
        dialer.connect(str(agent_writable / "into-root" / "control.sock"), HANG_GUARD_S).close()
        with pytest.raises(OperatorSocketOutsideRootError, match="runtime root"):
            dialer.connect(str(agent_writable / "out-of-root" / "control.sock"), HANG_GUARD_S)

    threads.run(main)

    assert network.dialed == [str((root / "session").resolve() / "control.sock")]


def test_a_user_dialer_reads_the_key_as_it_connects_and_dials_only_its_runtime_root(
    tmp_path: Path,
) -> None:
    environ = _environ(tmp_path)
    threads = SimThreads(schedule_seed=0)
    network = _RecordingNetwork(threads)
    dialer = OperatorDialer.for_user(network, environ, _UID)
    root = Path(environ["XDG_RUNTIME_DIR"]) / "vibesys"
    address = str(root / "control.sock")

    def main() -> bytes:
        with pytest.raises(OperatorKeyError, match="XDG_RUNTIME_DIR"):
            dialer.connect(address, HANG_GUARD_S)
        key = create_operator_key(environ, _UID)
        listener = network.listen(address)
        dialer.connect(address, HANG_GUARD_S).close()
        with pytest.raises(OperatorSocketOutsideRootError):
            dialer.connect(str(tmp_path / "control.sock"), HANG_GUARD_S)
        server = listener.accept(HANG_GUARD_S)
        received = server.recv(4096, HANG_GUARD_S)
        assert key.admits(received)
        return received

    threads.run(main)

    assert network.dialed == [str(root.resolve() / "control.sock")]


def _user_root(tmp_path: Path) -> tuple[dict[str, str], Path, OperatorKey]:
    """A user's environment, its runtime root and the key a server made there."""
    environ = _environ(tmp_path)
    key = create_operator_key(environ, _UID)
    return environ, Path(environ["XDG_RUNTIME_DIR"]) / "vibesys", key


def _replace_root(root: Path, unsafe: str) -> None:
    """Make ``root`` unsafe in the way named, with a key and a listener's directory in it."""
    match unsafe:
        case "symlink":
            # The reviewer's probe: the root is a link to a directory anyone can write.
            planted = root.parent / "planted"
            planted.mkdir(mode=0o777)
            planted.chmod(0o777)
            (root / "control.key").rename(planted / "control.key")
            root.rmdir()
            root.symlink_to(planted, target_is_directory=True)
        case "group-readable":
            root.chmod(0o750)
        case "world-writable":
            root.chmod(0o777)
        case _:
            raise AssertionError(unsafe)


@pytest.mark.parametrize("unsafe", ["symlink", "group-readable", "world-writable"])
def test_a_user_dialer_refuses_a_root_that_is_not_a_private_directory(
    tmp_path: Path, unsafe: str
) -> None:
    """Checked on every dial, before the key is read and before anything is connected."""
    environ, root, _ = _user_root(tmp_path)
    threads = SimThreads(schedule_seed=0)
    network = _RecordingNetwork(threads)
    dialer = OperatorDialer.for_user(network, environ, _UID)
    address = str(root / "control.sock")

    def main() -> None:
        network.listen(address)
        dialer.connect(address, HANG_GUARD_S).close()
        _replace_root(root, unsafe)
        with pytest.raises(OperatorKeyError, match="runtime root"):
            dialer.connect(address, HANG_GUARD_S)

    threads.run(main)

    assert len(network.dialed) == 1


def test_a_user_dialer_refuses_a_root_another_uid_owns(tmp_path: Path) -> None:
    """Owner and mode are read from the opened root; here the dialer's uid is not its owner."""
    _, root, _ = _user_root(tmp_path)
    network = _RecordingNetwork(SimThreads(schedule_seed=0))
    dialer = OperatorDialer(network, root=root, owner=_UID + 1)

    with pytest.raises(OperatorKeyError, match=f"not owned by uid {_UID + 1}"):
        dialer.connect(str(root / "control.sock"), HANG_GUARD_S)

    assert network.dialed == []


def test_a_dialer_takes_a_key_or_an_owner_but_not_both(tmp_path: Path) -> None:
    network = SimNetwork(SimThreads(schedule_seed=0))
    with pytest.raises(ValueError, match="not both"):
        OperatorDialer(network, root=tmp_path)
    with pytest.raises(ValueError, match="not both"):
        OperatorDialer(network, OperatorKey.generate(), root=tmp_path, owner=_UID)
