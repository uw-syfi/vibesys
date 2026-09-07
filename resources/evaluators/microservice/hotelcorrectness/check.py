"""Run the Python Hotel Reservation correctness gate."""

# ruff: noqa: C901, D102, E402, D103, D107, PLR2004, S310, S603, TC003, TRY003

from __future__ import annotations

import argparse
import http.client
import subprocess
import sys
import time
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from urllib.parse import urlencode, urlsplit

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hotelcorrectness.hotel_suite import REQUIRED_PROPERTIES as _REQUIRED_PROPERTIES
from hotelcorrectness.hotel_suite import SUITE

REQUIRED_PROPERTIES = _REQUIRED_PROPERTIES

from vs_correctness import (
    ActionResult,
    CustomAction,
    Environment,
    HTTPAction,
    HTTPExecutor,
    Observation,
    TestCase,
    Verifier,
    gate_exit_code,
    write_report,
)


def _run(command: list[str], *, cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True)


READINESS_PROBES = {
    "auth": "http://localhost:5000/user?username=Cornell_30&password=0000000000",
    "recommendations": (
        "http://localhost:5000/recommendations?require=rate&lat=37.7867&lon=-122.4112"
    ),
    "hotels": (
        "http://localhost:5000/hotels?inDate=2300-01-01&outDate=2300-01-02"
        "&lat=37.7867&lon=-122.4112"
    ),
}


class HotelExecutor:
    """Execute HTTP histories and serialized service restart fault actions."""

    def __init__(
        self,
        compose_services: tuple[str, ...],
        restart: Callable[[], None],
    ) -> None:
        self._compose_services = compose_services
        self._restart = restart

    def execute(self, test_case: TestCase, environment: Environment) -> Observation:
        if test_case.setup or test_case.cleanup:
            raise ValueError("hotel executor does not support setup or cleanup actions")
        results: list[ActionResult] = []
        segment: list[HTTPAction] = []
        segment_start = 0

        def flush() -> bool:
            nonlocal segment
            if not segment:
                return True
            observation = HTTPExecutor().execute(
                TestCase(id=f"{test_case.id}:{segment_start}", actions=tuple(segment)), environment
            )
            results.extend(
                result.model_copy(update={"index": segment_start + result.index})
                for result in observation.action_results
            )
            segment = []
            return not observation.failed

        for index, action in enumerate(test_case.actions):
            if isinstance(action, HTTPAction):
                if not segment:
                    segment_start = index
                segment.append(action)
                continue
            if not flush():
                break
            if isinstance(action, CustomAction) and action.name == "restart-services":
                started = time.monotonic()
                try:
                    self._restart()
                    result = ActionResult(
                        phase="actions",
                        index=index,
                        status=204,
                        elapsed_seconds=time.monotonic() - started,
                    )
                except Exception as error:  # noqa: BLE001
                    result = ActionResult(
                        phase="actions",
                        index=index,
                        elapsed_seconds=time.monotonic() - started,
                        error=f"restart failed: {type(error).__name__}: {error}",
                    )
            elif isinstance(action, CustomAction) and action.name == "persistent-http":
                result = self._persistent_http_probe(environment, index)
            else:
                result = ActionResult(
                    phase="actions", index=index, error=f"unsupported hotel action {action!r}"
                )
            results.append(result)
            if result.error is not None:
                break
        else:
            flush()
        return Observation(
            environment=environment,
            action_results=tuple(results),
            artifacts={"compose_services": list(self._compose_services)},
        )

    @staticmethod
    def _persistent_http_probe(environment: Environment, index: int) -> ActionResult:
        """Require two valid responses on the same live HTTP connection."""
        started = time.monotonic()
        parsed = urlsplit(environment.base_url)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=5)
        username, password = "Cornell_30", "0000000000"
        target = f"/user?{urlencode({'username': username, 'password': password})}"
        try:
            connection.request("GET", target)
            first = connection.getresponse()
            first_body = first.read()
            first_socket = connection.sock
            connection.request("GET", target)
            second = connection.getresponse()
            second_body = second.read()
            second_socket = connection.sock
            if first.status != 200 or second.status != 200 or first_body != second_body:
                return ActionResult(
                    phase="actions",
                    index=index,
                    status=412,
                    body="persistent HTTP probe responses differ or are not HTTP 200",
                    elapsed_seconds=time.monotonic() - started,
                )
            if first_socket is None or second_socket is not first_socket:
                return ActionResult(
                    phase="actions",
                    index=index,
                    status=412,
                    body="frontend did not reuse the HTTP connection",
                    elapsed_seconds=time.monotonic() - started,
                )
            return ActionResult(
                phase="actions",
                index=index,
                status=204,
                elapsed_seconds=time.monotonic() - started,
            )
        except Exception as error:  # noqa: BLE001
            return ActionResult(
                phase="actions",
                index=index,
                elapsed_seconds=time.monotonic() - started,
                error=f"persistent HTTP probe failed: {type(error).__name__}: {error}",
            )
        finally:
            connection.close()


def _compose_services(candidate_dir: Path, compose: list[str]) -> tuple[str, ...]:
    result = subprocess.run(
        [*compose, "config", "--services"],
        cwd=candidate_dir,
        check=True,
        capture_output=True,
        text=True,
    )
    return tuple(line for line in result.stdout.splitlines() if line)


def _wait_ready(
    probes: Mapping[str, str],
    timeout: float,
    *,
    consecutive_successes: int = 2,
    retry_interval: float = 1.0,
) -> None:
    deadline = time.monotonic() + timeout
    successful_sweeps = 0
    last_results: dict[str, str] = dict.fromkeys(probes, "not attempted")
    while time.monotonic() < deadline:
        sweep_passed = True
        for name, url in probes.items():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                sweep_passed = False
                break
            try:
                with urllib.request.urlopen(url, timeout=min(2, remaining)) as response:
                    last_results[name] = f"HTTP {response.status}"
                    if response.status != 200:
                        sweep_passed = False
            except Exception as error:  # noqa: BLE001
                last_results[name] = f"{type(error).__name__}: {error}"
                sweep_passed = False
        successful_sweeps = successful_sweeps + 1 if sweep_passed else 0
        if successful_sweeps >= consecutive_successes:
            return
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(retry_interval, remaining))
    details = ", ".join(f"{name}={result}" for name, result in last_results.items())
    raise TimeoutError(f"candidate did not become ready within {timeout:g} seconds ({details})")


def _probe_available(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            return response.status == 200
    except Exception:  # noqa: BLE001
        return False


def _wait_stopped(probes: Mapping[str, str], timeout: float) -> None:
    """Require a full probe sweep to observe the stopped application."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not any(_probe_available(url) for url in probes.values()):
            return
        time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    raise TimeoutError(f"candidate endpoints remained available {timeout:g} seconds after stop")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-dir", type=Path, required=True)
    parser.add_argument("--compose-file", action="append", type=Path, default=[])
    parser.add_argument("--compose-project-name")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--cases", type=int, default=4)
    parser.add_argument("--report", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.cases < 1:
        parser.error("--cases must be positive")
    candidate_dir = arguments.candidate_dir.resolve()
    compose = ["docker", "compose"]
    if arguments.compose_project_name:
        compose.extend(["--project-name", arguments.compose_project_name])
    for compose_file in arguments.compose_file:
        compose.extend(["--file", str(compose_file.resolve())])
    compose_services = _compose_services(candidate_dir, compose)

    def reset() -> None:
        _run([*compose, "down", "-v", "--remove-orphans"], cwd=candidate_dir)
        _run([*compose, "up", "-d", "--build"], cwd=candidate_dir)
        _wait_ready(READINESS_PROBES, 120)

    def restart() -> None:
        _run(
            [
                *compose,
                "stop",
                "-t",
                "10",
                "frontend",
                "geo",
                "profile",
                "rate",
                "recommendation",
                "reservation",
                "search",
                "user",
            ],
            cwd=candidate_dir,
        )
        _wait_stopped(READINESS_PROBES, 30)
        _run([*compose, "up", "-d"], cwd=candidate_dir)
        _wait_ready(READINESS_PROBES, 120)

    _run([*compose, "down", "-v", "--remove-orphans"], cwd=candidate_dir)
    try:
        report = Verifier(
            HotelExecutor(compose_services, restart),
            reset=lambda _environment: reset(),
            max_shrink_attempts=0,
        ).verify(
            SUITE,
            candidate=Environment(name="candidate", base_url="http://localhost:5000"),
            seed=arguments.seed,
            cases=arguments.cases + 4,
        )
        write_report(report, arguments.report)
        if gate_exit_code(report):
            return 1
    finally:
        _run([*compose, "down", "-v", "--remove-orphans"], cwd=candidate_dir)

    return 0


if __name__ == "__main__":
    sys.exit(main())
