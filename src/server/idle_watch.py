"""How long a server has had no client: the decision that ends a read-only server nobody holds.

A pure fold over the readings the server's shell takes (how many clients are connected, and what
time it is). It reads no clock and holds no thread, so the decision is checked on generated
readings and the shell that takes them runs on any clock, a simulated one included.

Public surface: ``IdleWatch``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class IdleWatch:
    """The time a server has been idle, and how long it may stay so.

    ``idle_since`` is when the last client left, or when the watch began while none has come;
    ``None`` while a client holds a connection. A server nobody ever connects to ends ``bound``
    seconds after it began, so a launch whose client never arrived does not leave one running.
    """

    bound: float
    idle_since: float | None

    @classmethod
    def begin(cls, bound: float, now: float) -> IdleWatch:
        """A watch that starts at ``now`` with no client yet."""
        return cls(bound=bound, idle_since=now)

    def observe(self, clients: int, now: float) -> IdleWatch:
        """The watch after seeing ``clients`` connections at ``now``."""
        if clients > 0:
            return IdleWatch(bound=self.bound, idle_since=None)
        if self.idle_since is None:
            return IdleWatch(bound=self.bound, idle_since=now)
        return self

    def expired(self, now: float) -> bool:
        """Whether the server has been idle for ``bound`` seconds by ``now``."""
        return self.idle_since is not None and now - self.idle_since >= self.bound
