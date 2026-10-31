"""HTTPS GET requests to a public server, behind a deterministic interface."""

from __future__ import annotations

import urllib.error
import urllib.parse
import urllib.request
from typing import IO, TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from http.client import HTTPMessage

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


class HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to another ``https`` address, so a response cannot downgrade the fetch."""

    def redirect_request(  # noqa: PLR0913  # lint-waiver: LW-306119 [PLR0913]; the signature of the standard library's hook, which this overrides.
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> urllib.request.Request | None:
        """The request for ``newurl``; ``HttpsGetError`` when it is not an ``https`` address."""
        if urllib.parse.urlsplit(newurl).scheme != _SCHEME:
            message = f"{req.full_url[:60]} redirected to a non-https address"
            raise HttpsGetError(message)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class UrllibHttpsGet:
    """:class:`HttpsGet` over the standard library's HTTP client.

    ``timeout`` bounds each wait on the connection, not the whole fetch: a server that sends a
    byte at a time can hold one fetch longer, so a caller that needs a bound over the whole call
    (the host's gateway does) keeps its own deadline.
    """

    def get(self, url: str, *, accept: str, timeout: float, max_bytes: int) -> bytes:
        """Send the request with ``urllib``; any failure to get a whole 200 body is an error."""
        if urllib.parse.urlsplit(url).scheme != _SCHEME:
            message = f"only https addresses are fetched, not {url[:40]!r}"
            raise HttpsGetError(message)
        request = urllib.request.Request(url, headers={"Accept": accept})  # noqa: S310  # lint-waiver: LW-306117 [S310]; the scheme is checked to be https above
        opener = urllib.request.build_opener(HttpsOnlyRedirects)
        try:
            with opener.open(request, timeout=timeout) as response:
                body = response.read(max_bytes + 1)
                status = response.status
        except HttpsGetError:
            raise
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
