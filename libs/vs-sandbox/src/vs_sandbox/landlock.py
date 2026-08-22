r"""Unprivileged filesystem confinement via the Linux Landlock LSM.

Landlock (kernel 5.13+) lets an *unprivileged* process restrict its own
filesystem access, and every process it later spawns inherits the restriction
irreversibly. Unlike bubblewrap it needs neither root nor an unprivileged user
namespace, so it is the one host confinement mechanism still available where
``CLONE_NEWUSER`` is blocked. Ubuntu's
``kernel.apparmor_restrict_unprivileged_userns`` is the common case: it denies
the uid-map write for any binary without an AppArmor profile granting
``userns``, which the distro ships only for ``/usr/bin/bwrap`` itself.

This module has two roles, kept together because they share one contract:

* :func:`restrict` applies a :class:`LandlockPolicy` to the current process.
* ``python -m vs_sandbox.landlock --policy JSON -- COMMAND ...`` applies a
  policy and then ``execvp``\\ s the command. That is the argv
  :meth:`~vs_sandbox.host_sandbox.LandlockSandbox.wrap` emits.

Two properties decide what this backend can and cannot enforce:

* Landlock is an access check, not a namespace. A denied path still exists and
  still ``stat``\\ s; only the ``open`` fails, with ``EACCES``.
* **Rules are additive.** A rule grants rights over a whole hierarchy, and a
  rule on a nested path can only *add* rights, never remove them. Granting the
  workspace ``WRITE_FILE`` therefore grants it to every descendant, and no
  nested rule can take it back. Stacking rulesets does not help: each layer is
  itself additive, so the layer that must grant ``MAKE_REG`` on the project
  root to let the agent create files there grants it throughout the subtree.

The practical consequence is that this backend enforces the *outer* boundary
(the project is writable, the rest of the host is not) and cannot express the
nested read-only or hidden tiers of a
:class:`~vs_sandbox.project_paths.ProjectPathPolicy`. Those need bubblewrap's
mount overlays. See :class:`~vs_sandbox.host_sandbox.LandlockSandbox`, which
reports the tiers it leaves unenforced rather than dropping them silently.

Two access types are deliberately left unhandled, so the kernel does not
restrict them at all:

``LANDLOCK_ACCESS_FS_IOCTL_DEV`` (ABI 5)
    Handling it would deny ``ioctl`` on every device node not explicitly
    granted, breaking terminals and accelerator devices (``/dev/neuron*``,
    ``/dev/nvidia*``). Bubblewrap does not filter ioctls either, so leaving it
    unhandled is the parity choice.

network access (ABI 4)
    Network stays open on both backends so the agent can reach its model
    provider. Filesystem confinement is what blocks the escape.
"""

from __future__ import annotations

import argparse
import ctypes
import enum
import os
import platform
import stat
import sys
from ctypes import c_int, c_long, c_size_t, c_uint32, c_uint64
from pathlib import Path  # noqa: TC003  # tracked: #288

from pydantic import BaseModel, ConfigDict

# ``linux/landlock.h``. Bits 0-12 are ABI 1; REFER is ABI 2, TRUNCATE ABI 3.
_ABI_REFER = 2
_ABI_TRUNCATE = 3
_ABI_SCOPED = 6

# ``LANDLOCK_SCOPE_*`` (ABI 6). Scoping closes two channels that bypass the
# filesystem entirely, and that bubblewrap also leaves open because it shares
# the network namespace: abstract unix sockets are addressed by name rather
# than by path, and signals reach any process of the same uid. Both let a
# confined agent reach an *unconfined* process and ask it to act.
_LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0
_LANDLOCK_SCOPE_SIGNAL = 1 << 1

# Landlock's three syscalls landed together in the generic syscall table, so
# these numbers hold for every architecture that adopted it at that revision.
# Architectures with bespoke numbering (alpha, mips, ...) are refused rather
# than guessed, because a wrong number calls an unrelated syscall.
_SYSCALL_NUMBERS: dict[str, tuple[int, int, int]] = dict.fromkeys(
    ("x86_64", "aarch64", "riscv64", "ppc64le", "s390x", "armv7l", "armv8l"),
    (444, 445, 446),
)

_PR_SET_NO_NEW_PRIVS = 38
_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
_LANDLOCK_RULE_PATH_BENEATH = 1


class LandlockUnavailableError(RuntimeError):
    """Raised when Landlock cannot confine this process on this host."""


class AccessFS(enum.IntFlag):
    """``LANDLOCK_ACCESS_FS_*`` bits from ``linux/landlock.h``."""

    EXECUTE = 1 << 0
    WRITE_FILE = 1 << 1
    READ_FILE = 1 << 2
    READ_DIR = 1 << 3
    REMOVE_DIR = 1 << 4
    REMOVE_FILE = 1 << 5
    MAKE_CHAR = 1 << 6
    MAKE_DIR = 1 << 7
    MAKE_REG = 1 << 8
    MAKE_SOCK = 1 << 9
    MAKE_FIFO = 1 << 10
    MAKE_BLOCK = 1 << 11
    MAKE_SYM = 1 << 12
    REFER = 1 << 13
    TRUNCATE = 1 << 14


_READ_BITS = AccessFS.EXECUTE | AccessFS.READ_FILE | AccessFS.READ_DIR
_WRITE_BITS = (
    AccessFS.WRITE_FILE
    | AccessFS.REMOVE_DIR
    | AccessFS.REMOVE_FILE
    | AccessFS.MAKE_CHAR
    | AccessFS.MAKE_DIR
    | AccessFS.MAKE_REG
    | AccessFS.MAKE_SOCK
    | AccessFS.MAKE_FIFO
    | AccessFS.MAKE_BLOCK
    | AccessFS.MAKE_SYM
    | AccessFS.REFER
    | AccessFS.TRUNCATE
)


class RuleAccess(enum.StrEnum):
    """Access tier granted over one hierarchy.

    There is deliberately no "hidden" tier. Because rules only ever add rights,
    a path is denied by *omitting* it, which works only when no granted tree
    contains it. A path nested inside a granted tree cannot be taken back.
    """

    #: Read, write, create, and delete. Comparable to bubblewrap ``--bind``.
    FULL = "full"
    #: Read and execute only. Comparable to bubblewrap ``--ro-bind``, but only
    #: for a tree that no other rule already grants write access to.
    READ = "read"


_ACCESS_BITS: dict[RuleAccess, AccessFS] = {
    RuleAccess.FULL: _READ_BITS | _WRITE_BITS,
    RuleAccess.READ: _READ_BITS,
}

# Rights that mean something for a non-directory. Granting any of the others on
# a regular file makes ``landlock_add_rule`` fail with ``EINVAL`` rather than
# ignoring them, so a file rule is masked down to these.
_FILE_BITS = AccessFS.EXECUTE | AccessFS.WRITE_FILE | AccessFS.READ_FILE | AccessFS.TRUNCATE


class LandlockRule(BaseModel):
    """One path and the access tier granted beneath it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    access: RuleAccess


class LandlockPolicy(BaseModel):
    """A complete ruleset, serialized across the ``wrap()`` process boundary.

    Rule order does not matter, and not because a nested rule wins: rights from
    every rule covering a path are unioned, so the result is order-independent.
    Overlapping rules widen access rather than narrowing it, which is why
    callers must keep granted trees disjoint to keep a path denied.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    rules: tuple[LandlockRule, ...] = ()
    chdir: str | None = None


def _libc() -> ctypes.CDLL:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = c_long
    libc.prctl.restype = c_int
    return libc


def _syscall_numbers() -> tuple[int, int, int]:
    machine = platform.machine()
    numbers = _SYSCALL_NUMBERS.get(machine)
    if numbers is None:
        raise LandlockUnavailableError(  # noqa: TRY003  # tracked: #288
            f"no known Landlock syscall numbers for architecture {machine!r}"
        )
    return numbers


def abi_version() -> int | None:
    """Return the kernel's Landlock ABI version, or ``None`` if unsupported.

    ``None`` covers every reason the LSM cannot be used: a pre-5.13 kernel, a
    kernel built without Landlock, a boot that left it out of the active LSM
    list, and an architecture whose syscall numbers we refuse to guess.
    """
    if sys.platform != "linux":
        return None
    try:
        create, _add, _restrict = _syscall_numbers()
    except LandlockUnavailableError:
        return None
    libc = _libc()
    version = libc.syscall(
        c_long(create),
        ctypes.c_void_p(None),
        c_size_t(0),
        c_uint32(_LANDLOCK_CREATE_RULESET_VERSION),
    )
    if version <= 0:
        return None
    return int(version)


def _handled_access(abi: int) -> AccessFS:
    """Return the access bits to govern, masked to what *abi* understands.

    Handling a bit the kernel does not know makes ``landlock_create_ruleset``
    fail with ``EINVAL``, so newer rights are dropped on older kernels. The
    cost of dropping one is that the kernel stops mediating that operation:
    without TRUNCATE, ``ftruncate`` on a read-only path is not denied.
    """
    handled = _ACCESS_BITS[RuleAccess.FULL]
    if abi < _ABI_TRUNCATE:
        handled &= ~AccessFS.TRUNCATE
    if abi < _ABI_REFER:
        handled &= ~AccessFS.REFER
    return handled


class _RulesetAttr(ctypes.Structure):
    """``struct landlock_ruleset_attr``, truncated to its filesystem field.

    Passing ``sizeof`` of just the first field tells the kernel to read only
    ``handled_access_fs``, which keeps one struct valid on every pre-ABI-6
    kernel that has not yet appended the network and scoping fields.
    """

    _fields_ = (("handled_access_fs", c_uint64),)


class _ScopedRulesetAttr(ctypes.Structure):
    """``struct landlock_ruleset_attr`` including the ABI 6 ``scoped`` field.

    ``handled_access_net`` stays zero: network access is deliberately
    unrestricted so the agent can reach its model provider.
    """

    _fields_ = (
        ("handled_access_fs", c_uint64),
        ("handled_access_net", c_uint64),
        ("scoped", c_uint64),
    )


class _PathBeneathAttr(ctypes.Structure):
    """``struct landlock_path_beneath_attr``, which the kernel declares packed."""

    _pack_ = 1
    _fields_ = (("allowed_access", c_uint64), ("parent_fd", c_int))


def supports_scoping() -> bool:
    """Return whether this kernel can scope signals and abstract unix sockets.

    Those are the two channels a confined process could otherwise use to make
    an unconfined one act for it. Scoping arrived in ABI 6; below that they
    stay open, which is also where bubblewrap sits for abstract sockets.
    """
    abi = abi_version()
    return abi is not None and abi >= _ABI_SCOPED


def _ruleset_attr(abi: int, handled: AccessFS) -> ctypes.Structure:
    """Return the ruleset attribute struct this kernel's ABI understands.

    On ABI 6 and later the struct carries ``scoped``, which closes the two
    non-filesystem channels a confined process could otherwise use to reach an
    unconfined one. Older kernels get the truncated filesystem-only struct and
    keep those channels open, exactly as bubblewrap does.
    """
    if abi < _ABI_SCOPED:
        return _RulesetAttr(handled_access_fs=c_uint64(int(handled)))
    return _ScopedRulesetAttr(
        handled_access_fs=c_uint64(int(handled)),
        handled_access_net=c_uint64(0),
        scoped=c_uint64(_LANDLOCK_SCOPE_ABSTRACT_UNIX_SOCKET | _LANDLOCK_SCOPE_SIGNAL),
    )


def restrict(policy: LandlockPolicy) -> int:
    """Apply *policy* to the current process and return the ABI version used.

    The restriction covers this process and every descendant, and cannot be
    lifted. Rules naming a path that does not exist are skipped, matching
    bubblewrap's ``--ro-bind-try`` tolerance for host layouts that vary.

    Raises:
        LandlockUnavailableError: If the kernel cannot enforce the policy. The
            caller must treat this as fail-closed; a partially applied ruleset
            is never left in place, because ``landlock_restrict_self`` is the
            single point where anything takes effect.
    """
    abi = abi_version()
    if abi is None:
        raise LandlockUnavailableError(  # noqa: TRY003  # tracked: #288
            "kernel does not support Landlock"
        )
    create, add_rule, restrict_self = _syscall_numbers()
    libc = _libc()

    handled = _handled_access(abi)
    attr = _ruleset_attr(abi, handled)
    ruleset_fd = libc.syscall(
        c_long(create),
        ctypes.byref(attr),
        c_size_t(ctypes.sizeof(attr)),
        c_uint32(0),
    )
    if ruleset_fd < 0:
        raise LandlockUnavailableError(  # noqa: TRY003  # tracked: #288
            f"landlock_create_ruleset failed: {os.strerror(ctypes.get_errno())}"
        )

    try:
        for rule in policy.rules:
            _add_path_rule(libc, add_rule, int(ruleset_fd), rule, handled=handled)
        if libc.prctl(c_int(_PR_SET_NO_NEW_PRIVS), c_long(1), c_long(0), c_long(0), c_long(0)) != 0:
            raise LandlockUnavailableError(  # noqa: TRY003  # tracked: #288
                f"prctl(PR_SET_NO_NEW_PRIVS) failed: {os.strerror(ctypes.get_errno())}"
            )
        if libc.syscall(c_long(restrict_self), c_int(int(ruleset_fd)), c_uint32(0)) != 0:
            raise LandlockUnavailableError(  # noqa: TRY003  # tracked: #288
                f"landlock_restrict_self failed: {os.strerror(ctypes.get_errno())}"
            )
    finally:
        os.close(int(ruleset_fd))
    return abi


def _add_path_rule(
    libc: ctypes.CDLL,
    syscall_number: int,
    ruleset_fd: int,
    rule: LandlockRule,
    *,
    handled: AccessFS,
) -> None:
    """Add one ``PATH_BENEATH`` rule, skipping paths absent from this host."""
    try:
        parent_fd = os.open(rule.path, os.O_PATH | os.O_CLOEXEC)
    except OSError:
        return
    try:
        # Granting a right the ruleset does not handle is EINVAL, so the tier's
        # bits are masked the same way ``_handled_access`` masked the ruleset.
        allowed = _ACCESS_BITS[rule.access] & handled
        if not stat.S_ISDIR(os.fstat(parent_fd).st_mode):
            allowed &= _FILE_BITS
        attr = _PathBeneathAttr(
            allowed_access=c_uint64(int(allowed)),
            parent_fd=c_int(parent_fd),
        )
        result = libc.syscall(
            c_long(syscall_number),
            c_int(ruleset_fd),
            c_uint32(_LANDLOCK_RULE_PATH_BENEATH),
            ctypes.byref(attr),
            c_uint32(0),
        )
        if result != 0:
            raise LandlockUnavailableError(  # noqa: TRY003  # tracked: #288
                f"landlock_add_rule failed for {rule.path}: {os.strerror(ctypes.get_errno())}"
            )
    finally:
        os.close(parent_fd)


def main(argv: list[str] | None = None) -> int:
    """Apply a JSON policy from argv, then ``execvp`` the command after ``--``."""
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--" not in raw:
        sys.stderr.write("usage: python -m vs_sandbox.landlock --policy JSON -- COMMAND [ARG...]\n")
        return 2
    separator = raw.index("--")
    parser = argparse.ArgumentParser(prog="python -m vs_sandbox.landlock", add_help=False)
    parser.add_argument("--policy", required=True)
    options = parser.parse_args(raw[:separator])
    command = raw[separator + 1 :]
    if not command:
        sys.stderr.write("no command given after '--'\n")
        return 2

    policy = LandlockPolicy.model_validate_json(options.policy)
    if policy.chdir is not None:
        os.chdir(policy.chdir)
    try:
        restrict(policy)
    except LandlockUnavailableError as exc:
        # Fail closed. Running the agent unconfined here would silently undo
        # the guarantee the caller asked ``build()`` to enforce.
        sys.stderr.write(f"[landlock] refusing to launch unconfined: {exc}\n")
        return 125
    try:
        os.execvp(command[0], command)  # noqa: S606  # tracked: #288
    except OSError as exc:
        sys.stderr.write(f"[landlock] cannot execute {command[0]}: {exc}\n")
        return 127


def policy_for(
    *,
    read_paths: tuple[Path, ...],
    write_paths: tuple[Path, ...],
    chdir: Path | None = None,
) -> LandlockPolicy:
    """Build a policy granting *read_paths* read access and *write_paths* full.

    Anything named by neither is denied, provided it is not nested inside one
    of the granted trees.
    """
    rules = [
        *(LandlockRule(path=str(path), access=RuleAccess.READ) for path in read_paths),
        *(LandlockRule(path=str(path), access=RuleAccess.FULL) for path in write_paths),
    ]
    return LandlockPolicy(rules=tuple(rules), chdir=str(chdir) if chdir is not None else None)


if __name__ == "__main__":
    raise SystemExit(main())
