"""HTTP requests to a server on this host's loopback interface, behind a deterministic interface."""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Mapping

_LOOPBACK_HOST = "127.0.0.1"


@dataclass(frozen=True)
class HttpReply:
    """What a server answered: its status and the start of its body."""

    status: int
    body: bytes


class LoopbackHttp(Protocol):
    """GET requests to a server listening on this host's loopback interface."""

    def get(
        self,
        port: int,
        target: str,
        *,
        timeout: float,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpReply | None:
        """GET ``http://127.0.0.1:{port}{target}``; ``None`` when nothing answered in time.

        ``target`` is the path and query, starting with ``/``; ``headers`` are sent with it.
        At most ``max_bytes`` of the body are read. A server that answers with an error
        status still answered.
        """
        ...


class UrllibLoopbackHttp:
    """:class:`LoopbackHttp` over the standard library's HTTP client."""

    def get(
        self,
        port: int,
        target: str,
        *,
        timeout: float,
        max_bytes: int,
        headers: Mapping[str, str] | None = None,
    ) -> HttpReply | None:
        """Send the request with ``urllib``; a refused or timed-out connection is ``None``."""
        if not target.startswith("/"):
            message = f"an HTTP target starts with '/', not {target[:1]!r}"
            raise ValueError(message)
        request = urllib.request.Request(
            f"http://{_LOOPBACK_HOST}:{port}{target}", headers=dict(headers or {})
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # lint-waiver: LW-101049 [S310]; the URL is always http on the loopback host, built above
                return HttpReply(response.status, response.read(max_bytes))
        except urllib.error.HTTPError as error:
            return HttpReply(error.code, error.read(max_bytes))
        except (OSError, urllib.error.URLError):
            return None
