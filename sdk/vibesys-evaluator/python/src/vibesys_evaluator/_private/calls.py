"""``Call``, ``Response`` and ``Versions``: what a check or an apply sees, and the visibility rule."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic

from vibesys_evaluator._private.typevars import K, V

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True, kw_only=True)
class Response:
    """One step's response.

    Attributes:
        status: The HTTP status; 0 when no response arrived.
        body: The body (operations in the validation sample keep it whole).
    """

    status: int
    body: bytes

    def json(self) -> Any:
        """The body parsed as JSON.

        Raises:
            ValueError: The body is not JSON (``json.JSONDecodeError``).
        """
        return json.loads(self.body)


@dataclass(frozen=True, kw_only=True)
class Call:
    """One operation as sent: what it was built from, when it ran and what came back.

    While a phase is generated, a write's ``apply`` sees it pending: no times,
    ``ok`` None and no responses.

    Attributes:
        info: The ``Request``'s info, as JSON (after a JSON round trip).
        start: When its first step was sent, Unix seconds; None while pending.
        end: When its last response was complete, Unix seconds; None while pending.
        ok: Every step answered with an expected status; None while pending.
        responses: One per step that was sent.
    """

    info: Mapping[str, Any]
    start: float | None
    end: float | None
    ok: bool | None
    responses: tuple[Response, ...]


@dataclass(frozen=True)
class _Write(Generic[V]):
    value: V
    start: float | None
    end: float | None
    ok: bool | None


class Versions(Generic[K, V]):
    """The values written to each key, with when, to say what a read may show.

    A read that ran over ``[start, end]`` may show, per key: the latest value
    acknowledged more than ``bound_secs`` before it started (the default if
    there is none), or any value whose write started before the read ended
    and was not certainly overwritten before the read started. A write that
    failed counts as possibly applied, at any time after it started.
    """

    __slots__ = ("_bound", "_writes")

    def __init__(self, *, bound_secs: float = 1.0) -> None:
        """Track the values of keys whose writes become visible within ``bound_secs``.

        Args:
            bound_secs: The staleness bound, at least 0.
        """
        if not bound_secs >= 0:
            msg = f"Versions bound_secs must be >= 0, got {bound_secs}"
            raise ValueError(msg)
        self._bound = float(bound_secs)
        self._writes: dict[K, list[_Write[V]]] = {}

    def write(self, key: K, value: V, call: Call) -> None:
        """Record that ``call`` wrote ``value`` to ``key`` (in application order per key).

        Args:
            key: The item.
            value: Its new value.
            call: The write, pending or with its times and outcome.
        """
        self._writes.setdefault(key, []).append(_Write(value, call.start, call.end, call.ok))

    def latest(self, key: K, default: V) -> V:
        """The last value written to ``key``, pending writes included; ``default`` if none.

        What generation builds on: the state the plan has sent so far.
        """
        writes = self._writes.get(key)
        return writes[-1].value if writes else default

    def allowed(self, key: K, default: V, *, start: float, end: float) -> tuple[V, ...]:
        """The values a read of ``key`` over ``[start, end]`` may show, oldest first, distinct.

        Args:
            key: The item.
            default: Its value before any write (the seeded one).
            start: When the read started, Unix seconds.
            end: When the read ended, Unix seconds.

        Returns:
            At least one value.
        """
        writes = [(w, w.start) for w in self._writes.get(key, ()) if w.start is not None]
        visible_by = start - self._bound
        # The latest start among the writes certainly applied and visible: every
        # acknowledged write that ended before it is certainly overwritten.
        settled = max(
            (began for w, began in writes if w.ok and w.end is not None and w.end <= visible_by),
            default=None,
        )
        values: list[V] = [] if settled is not None else [default]
        for w, began in writes:
            if began >= end:
                continue
            overwritten = settled is not None and w.ok and w.end is not None and w.end <= settled
            if not overwritten and w.value not in values:
                values.append(w.value)
        return tuple(values)
