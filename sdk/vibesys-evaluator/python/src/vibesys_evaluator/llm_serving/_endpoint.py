"""Where the candidate server is: the job environment's ``service_url``, set by vibesys-cli."""

from __future__ import annotations

import os
import re

from vibesys_evaluator._private import environment
from vibesys_evaluator._private.sample import SampleError
from vibesys_evaluator._proto.vibesys.cli.v1 import env_pb

_URL = re.compile(r"^https?://[^\s/]+$")


class EndpointError(SampleError):
    """The task container was not given the candidate server's URL."""


def endpoint() -> str:
    """The base URL of the candidate server, without a trailing slash.

    Raises:
        EndpointError: The job environment has no service address (it is unset or empty), or it is
            not an ``http(s)://host[:port]`` URL, or another variable of the job environment is
            malformed; the message names the variable.
    """
    name = environment.name_of(env_pb.JobEnvironment, "service_url")
    try:
        value = environment.job_environment(os.environ).service_url
    except environment.EnvironmentValueError as problem:
        if problem.variables != (name,):
            raise EndpointError(str(problem)) from problem
        msg = f"{name}={os.environ.get(name, '')!r} is not an http(s)://host[:port] URL"
        raise EndpointError(msg) from problem
    if not value:
        msg = (
            f"{name} is not set: vibesys-cli sets it to the candidate server's URL "
            "for the [accuracy], [benchmark] and [profile] commands of a task with "
            'version = "0.2"; to run bench.py by hand, set it to the URL of a running server '
            "(for example http://127.0.0.1:8000)"
        )
        raise EndpointError(msg)
    url = value.rstrip("/")
    if not _URL.match(url):
        msg = f"{name}={value!r} is not an http(s)://host[:port] URL"
        raise EndpointError(msg)
    return url
