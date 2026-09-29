"""Client helpers shared by the home server tests."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from entrypoints.web_home.context import HomeConfig


@dataclass(frozen=True)
class Reply:
    status: int
    body: bytes
    headers: Mapping[str, str]

    def json(self) -> dict[str, Any]:
        return json.loads(self.body)


@dataclass
class Home:
    config: HomeConfig
    workspace: Path
    default_headers: dict[str, str] = field(default_factory=dict)

    def send(
        self,
        method: str,
        path: str,
        body: object = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Reply:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(  # noqa: S310  # lint-waiver: LW-101304 [S310]; connect only to the loopback origin of the home server under test
            self.config.origin + path,
            data=data,
            method=method,
            headers=dict(self.default_headers if headers is None else headers),
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310  # lint-waiver: LW-101305 [S310]; connect only to the loopback request built above
                return Reply(response.status, response.read(), dict(response.headers))
        except urllib.error.HTTPError as error:
            return Reply(error.code, error.read(), dict(error.headers))

    def get(self, path: str) -> Reply:
        return self.send("GET", path)

    def post(self, path: str, body: object = None) -> Reply:
        return self.send("POST", path, {} if body is None else body)

    def put(self, path: str, body: object) -> Reply:
        return self.send("PUT", path, body)

    def delete(self, path: str) -> Reply:
        return self.send("DELETE", path)
