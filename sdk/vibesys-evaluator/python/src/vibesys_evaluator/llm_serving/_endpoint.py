"""Where the candidate server is: ``VIBESYS_SERVICE_URL``, set by vibesys-cli."""

from __future__ import annotations

import os
import re

from vibesys_evaluator._private.sample import SampleError

#: Set by vibesys-cli for every command of a two-image task's task container.
SERVICE_URL_ENV = "VIBESYS_SERVICE_URL"
_URL = re.compile(r"^https?://[^\s/]+$")


class EndpointError(SampleError):
    """The task container was not given the candidate server's URL."""


def endpoint() -> str:
    """The base URL of the candidate server, without a trailing slash.

    Raises:
        EndpointError: ``VIBESYS_SERVICE_URL`` is unset, empty or not an
            ``http(s)://host[:port]`` URL.
    """
    value = os.environ.get(SERVICE_URL_ENV, "").strip()
    if not value:
        msg = (
            f"{SERVICE_URL_ENV} is not set: vibesys-cli sets it to the candidate server's URL "
            "for the [accuracy], [benchmark] and [profile] commands of a task with "
            'version = "0.2"; to run bench.py by hand, set it to the URL of a running server '
            "(for example http://127.0.0.1:8000)"
        )
        raise EndpointError(msg)
    url = value.rstrip("/")
    if not _URL.match(url):
        msg = f"{SERVICE_URL_ENV}={value!r} is not an http(s)://host[:port] URL"
        raise EndpointError(msg)
    return url
