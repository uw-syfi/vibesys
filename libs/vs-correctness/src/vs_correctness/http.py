"""HTTP execution adapter for declarative correctness cases."""

from __future__ import annotations

import http.cookiejar
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import TYPE_CHECKING

from vs_correctness.models import Action, ActionResult, HTTPAction, Observation, Reference

if TYPE_CHECKING:
    from vs_correctness.models import Environment, TestCase


class HTTPExecutor:
    """Execute a case against one HTTP environment using a persistent session."""

    def execute(self, test_case: TestCase, environment: Environment) -> Observation:
        """Execute setup and actions, and always attempt cleanup."""
        results: list[ActionResult] = []
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        stopped = False
        try:
            for phase, actions in (("setup", test_case.setup), ("actions", test_case.actions)):
                for index, action in enumerate(actions):
                    result = self._invoke(opener, environment, action, phase, index, results)
                    results.append(result)
                    if result.error is not None:
                        stopped = True
                        break
                if stopped:
                    break
        finally:
            try:
                for index, action in enumerate(test_case.cleanup):
                    results.append(
                        self._invoke(opener, environment, action, "cleanup", index, results)
                    )
            finally:
                opener.close()
        return Observation(environment=environment, action_results=tuple(results))

    def _invoke(  # noqa: PLR0913
        self,
        opener: urllib.request.OpenerDirector,
        environment: Environment,
        action: Action,
        phase: str,
        index: int,
        prior: list[ActionResult],
    ) -> ActionResult:
        started = time.monotonic()
        if not isinstance(action, HTTPAction):
            return ActionResult(
                phase=phase,
                index=index,
                error=f"HTTPExecutor does not support action kind {action.kind!r}",
            )
        try:
            query_values = {
                key: [self._resolve(item, prior) for item in value]
                if isinstance(value, list)
                else self._resolve(value, prior)
                for key, value in action.query.items()
            }
            resolved_headers = {
                key: self._resolve(value, prior) for key, value in action.headers.items()
            }
            resolved_body = self._resolve(action.body, prior)
        except Exception as error:  # noqa: BLE001
            return ActionResult(
                phase=phase,
                index=index,
                elapsed_seconds=time.monotonic() - started,
                error=f"request construction failed: {type(error).__name__}: {error}",
            )
        try:
            query = urllib.parse.urlencode(query_values, doseq=True)
            url = urllib.parse.urljoin(
                environment.base_url.rstrip("/") + "/", action.path.lstrip("/")
            )
            if query:
                url = f"{url}?{query}"
            headers = resolved_headers
            data: bytes | None = None
            if resolved_body is not None:
                if isinstance(resolved_body, str):
                    data = resolved_body.encode()
                else:
                    data = json.dumps(resolved_body, separators=(",", ":")).encode()
                    headers.setdefault("Content-Type", "application/json")
            request = urllib.request.Request(  # noqa: S310
                url, data=data, headers=headers, method=action.method.upper()
            )
            with opener.open(request, timeout=action.timeout_seconds) as response:
                return self._response(response, phase, index, started)
        except urllib.error.HTTPError as error:
            return self._response(error, phase, index, started)
        except Exception as error:  # noqa: BLE001
            return ActionResult(
                phase=phase,
                index=index,
                elapsed_seconds=time.monotonic() - started,
                error=f"{type(error).__name__}: {error}",
            )

    @staticmethod
    def _response(response: object, phase: str, index: int, started: float) -> ActionResult:
        status = int(getattr(response, "status", getattr(response, "code", 0)))
        headers = {
            key.lower(): response.headers.get_all(key) or []  # type: ignore[attr-defined]
            for key in response.headers  # type: ignore[attr-defined]
        }
        body = response.read().decode("utf-8", errors="replace")  # type: ignore[attr-defined]
        return ActionResult(
            phase=phase,
            index=index,
            status=status,
            headers=headers,
            body=body,
            elapsed_seconds=time.monotonic() - started,
        )

    @staticmethod
    def _resolve(value: object, prior: list[ActionResult]) -> object:
        if not isinstance(value, Reference):
            return value
        matches = [result for result in prior if result.phase == value.phase]
        try:
            result = matches[value.index]
        except IndexError as error:
            raise LookupError(  # noqa: TRY003
                f"no {value.phase} result at index {value.index}"
            ) from error
        if value.source == "status":
            return result.status
        if value.source == "header":
            if not value.header:
                raise ValueError("header reference requires header")  # noqa: TRY003
            values = result.headers.get(value.header.lower())
            if not values:
                raise LookupError(f"response has no header {value.header!r}")  # noqa: TRY003
            return values[0]
        current: object = result.json_body()
        for part in value.json_path:
            if (isinstance(part, int) and isinstance(current, list)) or (
                isinstance(part, str) and isinstance(current, dict)
            ):
                current = current[part]
            else:
                message = f"reference part {part!r} is invalid for {type(current).__name__}"
                raise TypeError(message)
        return current
