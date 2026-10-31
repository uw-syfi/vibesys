"""HTTPS GET requests to a public server, behind a deterministic interface."""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from typing import Protocol

_SCHEME = "https"
_OK = 200


class HttpsGetError(Exception):
    """A GET that did not return a body: it failed, timed out, answered an error, or was too big."""


class HttpsGet(Protocol):
    """GET requests to a server on the public network, over TLS only."""

    def get(self, url: str, *, accept: str, timeout: float, max_bytes: int) -> bytes:
        """The body at ``url``, which must be an ``https`` address.

        The body is read whole or the call fails: ``HttpsGetError`` for a refused or timed-out
        connection, a status other than 200, a body longer than ``max_bytes`` (a cut document
        would be read as a whole one), and a ``url`` that is not ``https``.
        """
        ...


class UrllibHttpsGet:
    """:class:`HttpsGet` over the standard library's HTTP client."""

    def get(self, url: str, *, accept: str, timeout: float, max_bytes: int) -> bytes:
        """Send the request with ``urllib``; any failure to get a whole 200 body is an error."""
        if urllib.parse.urlsplit(url).scheme != _SCHEME:
            message = f"only https addresses are fetched, not {url[:40]!r}"
            raise HttpsGetError(message)
        request = urllib.request.Request(url, headers={"Accept": accept})  # noqa: S310  # lint-waiver: LW-306117 [S310]; the scheme is checked to be https above
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310  # lint-waiver: LW-306118 [S310]; the scheme is checked to be https above
                body = response.read(max_bytes + 1)
                status = response.status
        except urllib.error.HTTPError as error:
            message = f"{url[:60]} answered {error.code}"
            raise HttpsGetError(message) from error
        except (OSError, urllib.error.URLError) as error:
            message = f"{url[:60]} could not be reached: {error}"
            raise HttpsGetError(message) from error
        if status != _OK or len(body) > max_bytes:
            message = f"{url[:60]} answered {status} with a body over {max_bytes} bytes"
            raise HttpsGetError(message)
        return body
