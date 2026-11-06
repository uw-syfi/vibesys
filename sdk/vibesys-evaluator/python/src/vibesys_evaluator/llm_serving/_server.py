"""The OpenAI-compatible completions surface the golden check talks to, behind an interface.

``Completions`` is what the check needs from a server; ``HttpCompletions``
speaks it over HTTP with the standard library (an ``OpenerDirector``, so the
tests give it an in-process handler instead of a socket).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import TYPE_CHECKING, Any, Protocol

from vibesys_evaluator._private.sample import SampleError

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

_BODY_SHOWN = 500


class ServerError(SampleError):
    """The server answered a request with an error, or not in the protocol (the message says)."""


class Completions(Protocol):
    """``POST /v1/completions``, whole or streamed."""

    def complete(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        """The JSON response to a non-streamed request.

        Raises:
            ServerError: A status other than 200, or a body that is not a JSON object.
        """
        ...

    def stream(self, body: Mapping[str, Any]) -> Iterator[str]:
        """The lines of a streamed (server-sent events) response, without line ends.

        Raises:
            ServerError: A status other than 200.
        """
        ...


class HttpCompletions:
    """``Completions`` at ``base_url`` over HTTP."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_secs: float = 600.0,
        opener: urllib.request.OpenerDirector | None = None,
    ) -> None:
        """Send to ``base_url`` (no trailing slash) through ``opener`` (the default one if None)."""
        self._url = base_url.rstrip("/") + "/v1/completions"
        self._timeout = timeout_secs
        self._opener = opener or urllib.request.build_opener()

    def _open(self, body: Mapping[str, Any]) -> Any:
        request = urllib.request.Request(  # noqa: S310  # LW-392101 [S310]; the scheme is checked.
            # > endpoint() admits only http(s) URLs; ruff cannot see that check through the
            # > constructor, and urllib is the one HTTP client the package may depend on.
            self._url,
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            return self._opener.open(request, timeout=self._timeout)
        except urllib.error.HTTPError as error:
            text = error.read().decode(errors="replace")[:_BODY_SHOWN]
            msg = f"POST {self._url}: {error.code} from the server: {text}"
            raise ServerError(msg) from error
        except (urllib.error.URLError, OSError) as error:
            msg = f"POST {self._url}: {error}"
            raise ServerError(msg) from error

    def complete(self, body: Mapping[str, Any]) -> Mapping[str, Any]:
        """See ``Completions.complete``."""
        with self._open(body) as response:
            raw = response.read()
        try:
            value = json.loads(raw)
        except ValueError as error:
            msg = f"POST {self._url}: the response is not JSON: {raw[:_BODY_SHOWN]!r}"
            raise ServerError(msg) from error
        if not isinstance(value, dict):
            msg = f"POST {self._url}: the response is not a JSON object"
            raise ServerError(msg)
        return value

    def stream(self, body: Mapping[str, Any]) -> Iterator[str]:
        """See ``Completions.stream``."""
        with self._open(body) as response:
            for line in response:
                yield line.decode(errors="replace").rstrip("\r\n")
