"""The operator's key: the capability a run's control socket demands before it serves.

Whoever can connect to a run's control socket can steer, pause, stop and cancel the run,
and read its events. Being able to connect is not proof of being the operator: an agent
confined by Landlock or Seatbelt runs as the operator's uid and can connect to a pathname
socket it knows the path of (Landlock has no right for ``connect`` on a pathname socket,
and the Seatbelt profile allows ``network*``). So the socket admits a connection only
after it presents the operator's key (:meth:`OperatorKey.hello`), and the key is a file no
agent can read: mode ``0600`` in the user's runtime root (:func:`operator_key_path`),
which every agent confinement keeps out of the agent's view
(``vs_sandbox.operator_root``). The key is never passed in an
argument vector or an environment variable, which other processes can read.

One key serves every server of a user on a node, so a client finds it from the same
environment it finds the server registry from. Only a server creates it
(:func:`create_operator_key`), as it starts; a client only reads it
(:func:`read_operator_key`), so a client run with another ``XDG_RUNTIME_DIR`` than the
server's fails naming the file instead of minting a key no server accepts. It is never
rotated; deleting the file rotates it for servers started afterwards.

The key is only as private as the socket a client hands it to. A client therefore sends it
only to a socket inside the runtime root (:class:`OperatorDialer`), where no agent can
write: in a directory an agent can write, such as ``/tmp`` under Landlock, the agent could
move the real socket aside, bind its own in its place, and read the next client's hello.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Final, Self

from vs_project._private_directory import ensure_private_directory, user_runtime_root

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vs_sim.api import Connection, Network

OPERATOR_KEY_NAME: Final = "control.key"
"""The key's file name in the user's runtime root."""
CONTROL_HELLO_MAX_BYTES: Final = 128
"""The longest hello line a control socket reads before it refuses the connection."""
_HELLO_FIELD: Final = "control_key"
_SECRET_BYTES: Final = 32
_SECRET_HEX_CHARACTERS: Final = 2 * _SECRET_BYTES
_HEX_DIGITS: Final = frozenset("0123456789abcdef")
_KEY_MODE: Final = 0o600
_READ_FLAGS: Final = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY_FLAGS: Final = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)
_ROOT_MODE: Final = 0o700
_CREATE_FLAGS: Final = (
    os.O_WRONLY
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
)


class OperatorKeyError(PermissionError):
    """The key file is not a private regular file of the operator's holding one key."""

    @classmethod
    def unsafe(cls, path: Path, reason: str) -> OperatorKeyError:
        """Name the file and what is wrong with it, never its contents."""
        return cls(
            f"refusing the operator key {path}: {reason}; remove it and start the server "
            "again to make a new one"
        )

    @classmethod
    def unsafe_root(cls, root: Path, reason: str) -> OperatorKeyError:
        """Name the runtime root a client refuses to read the key from, and why."""
        return cls(
            f"refusing the operator's runtime root {root}: {reason}. It must be a directory "
            "owned by you with mode 0700, not a symlink; remove it if nothing of yours is "
            "using it and start the server again"
        )

    @classmethod
    def missing(cls, path: Path) -> OperatorKeyError:
        """No key where a client looks: no server made one, or it looks somewhere else."""
        return cls(
            f"no operator key at {path}: no vibesys server of yours has started with this "
            "XDG_RUNTIME_DIR. Start the server first, or run this client with the same "
            "XDG_RUNTIME_DIR as the server"
        )


class OperatorSocketOutsideRootError(PermissionError):
    """A client was asked to present the key to a socket outside the operator's runtime root."""

    def __init__(self, address: str, root: Path) -> None:
        """Name the socket and the root it must be in."""
        super().__init__(
            f"refusing to present the operator key to {address}: it is not inside the "
            f"operator's runtime root {root}, so a confined agent may have put it there. A "
            "run's control socket is always created inside that root"
        )


class OperatorKey:
    """One operator key; it never prints its secret.

    Contract: :meth:`admits` is true exactly for the line :meth:`hello` returns, compared
    in constant time, and false for every other byte string, malformed ones included.
    """

    __slots__ = ("_secret",)

    def __init__(self, secret: str) -> None:
        """Hold ``secret``, 64 lowercase hex digits; raise ``ValueError`` otherwise."""
        if len(secret) != _SECRET_HEX_CHARACTERS or not set(secret) <= _HEX_DIGITS:
            message = f"an operator key is {_SECRET_HEX_CHARACTERS} lowercase hex digits"
            raise ValueError(message)
        self._secret = secret

    @classmethod
    def generate(cls) -> Self:
        """Return a fresh random key."""
        return cls(_random_hex(_SECRET_BYTES))

    def hello(self) -> bytes:
        """The line a client sends first on every control socket connection."""
        return json.dumps({_HELLO_FIELD: self._secret}, separators=(",", ":")).encode() + b"\n"

    def admits(self, line: bytes) -> bool:
        """Whether ``line`` is this key's hello."""
        import hmac  # noqa: PLC0415  # lint-waiver: LW-313306 [PLC0415]; only a server admits, and the host helper imports this module to dial, where #2351 pins that answering hello loads no hmac (tests/e2e/test_host_helper.py). A module-level import would load it for every client; moving admits to a server-only module would split the key's one contract across two owners; hand-rolling a constant-time compare would trade a vetted primitive for an unvetted one.

        return hmac.compare_digest(line, self.hello())

    def __repr__(self) -> str:
        """Name the type only: a key in a log or a traceback would hand it out."""
        return "OperatorKey(<secret>)"


class OperatorDialer:
    """Connects to a control socket the way ``network`` does, and presents the key first.

    The one way an operator's client opens a control socket: every connection it returns
    has already sent :meth:`OperatorKey.hello`, so a request written next is served.

    Contract: the key goes only to a socket whose directory, resolved, is ``root`` or lies
    inside it, and the connection is made to that resolved path, so a symlink swapped in
    after the check cannot redirect it. Any other address raises
    :class:`OperatorSocketOutsideRootError` before anything is connected or sent. A dialer
    that reads its owner's key also refuses, with :class:`OperatorKeyError` and before
    anything is connected or sent, a root that is a symlink, not a directory, not the
    owner's, or not mode ``0700``.
    """

    def __init__(
        self,
        network: Network,
        key: OperatorKey | None = None,
        *,
        root: Path,
        owner: int | None = None,
    ) -> None:
        """Dial through ``network`` sockets inside ``root``, presenting a key.

        Give ``key`` to present a key already held; only where the socket is is checked.
        Give ``owner`` instead to read the key from ``root`` on every connect, after
        checking that the root itself is a real directory, not a symlink, owned by
        ``owner`` with mode ``0700``. The key is read through the directory just checked,
        so a root swapped for a symlink after the check cannot hand the dialer another key
        or send it to another listener. Raises ``ValueError`` unless exactly one is given.
        """
        if (key is None) == (owner is None):
            message = "an OperatorDialer presents a key it holds or reads its owner's, not both"
            raise ValueError(message)
        self._network = network
        self._key = key
        self._root = root
        self._owner = owner

    @classmethod
    def for_user(cls, network: Network, environ: Mapping[str, str], uid: int) -> Self:
        """A dialer for ``uid``'s servers: sockets in its runtime root, its key read per connect.

        The key is read (never created) as each connection opens, so a client may start
        before any server has made it.
        """
        return cls(network, root=user_runtime_root(environ, uid), owner=uid)

    def connect(self, address: str, timeout: float | None = None) -> Connection:
        """Connect to ``address`` and send the hello, both within ``timeout`` each.

        Raises :class:`OperatorSocketOutsideRootError` for an address outside the root,
        :class:`OperatorKeyError` when the root is not the user's private directory or the
        key cannot be read, and what ``network``'s ``connect`` and the connection's
        ``send`` raise; a connection whose hello could not be sent is closed first.
        """
        if self._key is not None:
            key = self._key
            target = self._inside_root(address, self._root.resolve())
        elif self._owner is not None:
            key, target = self._read_key_and_target(address, self._owner)
        else:  # pragma: no cover - the constructor demands a key or an owner.
            raise AssertionError(self)
        connection = self._network.connect(target, timeout)
        try:
            connection.send(key.hello(), timeout)
        except BaseException:
            connection.close()
            raise
        return connection

    def _read_key_and_target(self, address: str, owner: int) -> tuple[OperatorKey, str]:
        """Check the root, read the key through it, and resolve ``address`` inside it."""
        root = self._root
        try:
            descriptor = os.open(root, _DIRECTORY_FLAGS)
        except FileNotFoundError:
            raise OperatorKeyError.missing(root / OPERATOR_KEY_NAME) from None
        except OSError as error:
            raise OperatorKeyError.unsafe_root(
                root,
                f"it is not a directory one can open without following a link ({error.strerror})",
            ) from error
        try:
            status = os.fstat(descriptor)
            if status.st_uid != owner:
                raise OperatorKeyError.unsafe_root(root, f"it is not owned by uid {owner}")
            if stat.S_IMODE(status.st_mode) != _ROOT_MODE:
                raise OperatorKeyError.unsafe_root(root, "its mode is not 0700")
            key = _read(root / OPERATOR_KEY_NAME, owner, directory=descriptor)
            if key is None:
                raise OperatorKeyError.missing(root / OPERATOR_KEY_NAME)
            resolved = root.resolve()
            # The path the socket is checked against must still name the directory whose
            # key was read; one swapped in since would be another listener's.
            current = resolved.stat()
            if (current.st_dev, current.st_ino) != (status.st_dev, status.st_ino):
                raise OperatorKeyError.unsafe_root(root, "it was replaced while it was read")
        finally:
            os.close(descriptor)
        return key, self._inside_root(address, resolved)

    def _inside_root(self, address: str, root: Path) -> str:
        """``address`` with its directory resolved, if that directory is inside ``root``."""
        path = Path(address)
        directory = path.parent.resolve()
        if directory != root and root not in directory.parents:
            raise OperatorSocketOutsideRootError(address, self._root)
        return str(directory / path.name)


def operator_key_path(environ: Mapping[str, str], uid: int) -> Path:
    """Where ``uid``'s operator key lives: in the runtime root ``user_runtime_root`` names."""
    return user_runtime_root(environ, uid) / OPERATOR_KEY_NAME


def read_operator_key(environ: Mapping[str, str], uid: int) -> OperatorKey:
    """Return ``uid``'s operator key, as a client does: it never creates one.

    Raises :class:`OperatorKeyError` when the file is absent (naming ``XDG_RUNTIME_DIR``,
    the usual cause) or unsafe, as :func:`create_operator_key` does.
    """
    path = operator_key_path(environ, uid)
    key = _read(path, uid)
    if key is None:
        raise OperatorKeyError.missing(path)
    return key


def create_operator_key(environ: Mapping[str, str], uid: int) -> OperatorKey:
    """Return ``uid``'s operator key, creating the runtime root and the key if absent.

    Only a server calls this, before it binds its control socket; clients read the key
    with :func:`read_operator_key`.

    Concurrent first callers agree on one key: each writes a candidate to a private
    temporary file and links it into place, and whoever's link loses reads the winner's.
    Raises :class:`OperatorKeyError` if the file is a symlink, not a regular file, not
    ``uid``'s, readable or writable by group or other, or does not hold one key, and
    ``PrivateDirectoryError`` if the runtime root is not ``uid``'s private directory.
    """
    path = operator_key_path(environ, uid)
    ensure_private_directory(path.parent, uid)
    key = _read(path, uid)
    if key is not None:
        return key
    _publish(path, _random_hex(_SECRET_BYTES))
    published = _read(path, uid)
    if published is None:  # pragma: no cover - only a concurrent delete gets here.
        raise OperatorKeyError.unsafe(path, "it vanished as it was created")
    return published


def _read(path: Path, uid: int, *, directory: int | None = None) -> OperatorKey | None:
    """The key at ``path``, or ``None`` if absent; opened by name in ``directory`` if given."""
    try:
        descriptor = (
            os.open(path, _READ_FLAGS)
            if directory is None
            else os.open(path.name, _READ_FLAGS, dir_fd=directory)
        )
    except FileNotFoundError:
        return None
    except OSError as error:
        raise OperatorKeyError.unsafe(path, f"it cannot be opened ({error.strerror})") from error
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise OperatorKeyError.unsafe(path, "it is not a regular file")
        if status.st_uid != uid:
            raise OperatorKeyError.unsafe(path, f"it is not owned by uid {uid}")
        if stat.S_IMODE(status.st_mode) & 0o077:
            raise OperatorKeyError.unsafe(path, "group or other can access it")
        raw = os.read(descriptor, _SECRET_HEX_CHARACTERS + 2)
    finally:
        os.close(descriptor)
    try:
        return OperatorKey(raw.decode("ascii").removesuffix("\n"))
    except (UnicodeDecodeError, ValueError) as error:
        raise OperatorKeyError.unsafe(path, "it does not hold one key") from error


def _random_hex(count: int) -> str:
    """``count`` bytes from the OS CSPRNG as hex, as ``secrets.token_hex`` returns.

    Without importing ``secrets``, which the host helper must not load on hello (#2351).
    """
    return os.urandom(count).hex()


def _publish(path: Path, secret: str) -> None:
    candidate = path.with_name(f".{OPERATOR_KEY_NAME}.{_random_hex(8)}")
    descriptor = os.open(candidate, _CREATE_FLAGS, _KEY_MODE)
    try:
        os.write(descriptor, secret.encode() + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        os.link(candidate, path)
    except FileExistsError:
        pass
    finally:
        candidate.unlink()
