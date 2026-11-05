"""Field checks of `deploy.k8s.App`, and the parsers the deploy layer shares with them."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from vibesys_evaluator.deploy.k8s import App

NAME = re.compile(r"^[a-z]([a-z0-9-]{0,38}[a-z0-9])?$")
DNS_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
VARIABLE = re.compile(r"^[A-Z][A-Z0-9_]*$")
LABEL_KEY = re.compile(
    r"^([a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?/)?[A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?$"
)
LABEL_VALUE = re.compile(r"^([A-Za-z0-9]([A-Za-z0-9._-]{0,61}[A-Za-z0-9])?)?$")
CACHE_KINDS = ("memcached", "redis")


@dataclass(frozen=True)
class Gateway:
    """The gateway URL, split: the Service it names and its port."""

    url: str
    service: str
    port: int


def parse_gateway(url: str) -> Gateway:
    """`http://<service>:<port>` (port 80 when absent), with no path, query or credentials."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme != "http" or parts.path not in ("", "/") or parts.query or parts.username:
        raise ValueError(
            f"k8s.App(gateway={url!r}): want http://<service>:<port>, no path or query"
        )
    if not DNS_LABEL.match(host):
        raise ValueError(
            f"k8s.App(gateway={url!r}): {host!r} is not a Service name of the manifest"
        )
    try:
        port = parts.port or 80
    except ValueError as error:
        raise ValueError(f"k8s.App(gateway={url!r}): {error}") from None
    return Gateway(url=url.rstrip("/"), service=host, port=port)


def parse_selector(selector: str) -> dict[str, str]:
    """An equality label selector `key=value[,key=value]` (`==` too) as a dict; empty is {}."""
    labels: dict[str, str] = {}
    for raw in filter(None, (item.strip() for item in selector.split(","))):
        key, sep, value = raw.replace("==", "=").partition("=")
        key, value = key.strip(), value.strip()
        if not sep or key.endswith("!") or not LABEL_KEY.match(key) or not LABEL_VALUE.match(value):
            raise ValueError(
                f"k8s.App(data_tier={selector!r}): {raw!r} is not key=value "
                "(only equality selectors are supported)"
            )
        labels[key] = value
    return labels


def relative_path(field: str, value: str) -> None:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"k8s.App({field}={value!r}): want a relative path without '..'")


def check_app(app: App[Any]) -> None:
    """Raise ValueError naming the first field of an App that is wrong, and what to write."""
    if not NAME.match(app.name):
        raise ValueError(
            f"k8s.App(name={app.name!r}): want 1-40 lowercase letters, digits and dashes"
        )
    relative_path("manifest", app.manifest)
    _check_images(app.images)
    parse_gateway(app.gateway)
    if not app.ready:
        raise ValueError(
            "k8s.App(ready=[]): give at least one request that succeeds once the app serves"
        )
    if parse_selector(app.data_tier) and app.seed is None:
        raise ValueError(
            f"k8s.App(data_tier={app.data_tier!r}) orders the deploy around seed, "
            "but seed is None: remove data_tier or give seed"
        )
    _check_hooks(app.restartable, app.caches)


def _check_images(images: Mapping[str, str]) -> None:
    for variable, directory in images.items():
        if not VARIABLE.match(variable) or variable == "NAMESPACE":
            raise ValueError(
                f"k8s.App(images): key {variable!r} must be an UPPER_CASE manifest "
                "variable other than NAMESPACE"
            )
        relative_path(f"images[{variable!r}]", directory)


def _check_hooks(restartable: Sequence[str], caches: Mapping[str, str]) -> None:
    for deployment in restartable:
        if not DNS_LABEL.match(deployment):
            raise ValueError(f"k8s.App(restartable): {deployment!r} is not a Deployment name")
    if len(set(restartable)) != len(restartable):
        raise ValueError("k8s.App(restartable): names repeat")
    for cache, kind in caches.items():
        if not DNS_LABEL.match(cache):
            raise ValueError(f"k8s.App(caches): {cache!r} is not a Deployment name")
        if kind not in CACHE_KINDS:
            raise ValueError(f"k8s.App(caches[{cache!r}]={kind!r}): want one of {CACHE_KINDS}")
