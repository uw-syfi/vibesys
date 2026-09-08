import importlib.util
import json
import os
import subprocess
import sys
import tomllib
from collections.abc import Callable
from itertools import pairwise
from pathlib import Path
from types import ModuleType

import pytest
from pydantic import JsonValue

from vibesys.evaluators import PROJECT_ROOT_TOKEN
from vibesys.input_manifest import InputBundle, load_input_bundle, load_project_task
from vs_correctness import (
    ActionResult,
    CustomAction,
    Environment,
    GenerationContext,
    HTTPAction,
    Observation,
    OracleContext,
    Verdict,
)
from vs_correctness import (
    TestCase as CorrectnessTestCase,
)
from vs_project import Project, ProjectLayoutError

PROJECT_ROOT = Path(__file__).parents[2]
MICROSERVICE_ROOT = PROJECT_ROOT / "examples" / "microservices"
DEATHSTAR_ROOT = MICROSERVICE_ROOT / "repositories" / "deathstarbench"
# The DeathStarBench tasks live in a submodule, so a plain checkout does not
# have them and these assertions skip. CI's ``validate-examples`` job fetches
# the ``.vibesys`` overlay and sets this, turning a missing overlay into a
# failure rather than a silent loss of coverage.
_REQUIRE_EXAMPLE_OVERLAYS = os.environ.get("VIBESYS_REQUIRE_EXAMPLE_OVERLAYS") == "1"
try:
    DEATHSTAR_LAYOUT = Project.open(DEATHSTAR_ROOT)
    DEATHSTAR_LAYOUT.discover_tasks()
except ProjectLayoutError as error:
    if _REQUIRE_EXAMPLE_OVERLAYS:
        raise
    pytest.skip(
        f"DeathStarBench repository example is not initialized: {error}"
        " (set VIBESYS_REQUIRE_EXAMPLE_OVERLAYS=1 to force)",
        allow_module_level=True,
    )
DEATHSTAR_TASKS = {task.name.value: task for task in DEATHSTAR_LAYOUT.discover_tasks()}
LEGACY_SCENARIOS = (MICROSERVICE_ROOT / "train-ticket",)
HOTEL_TEMP_ROOT = Path("/") / "tmp" / "vibesys-hotel-reservation" / "otel"
HOTEL_CORRECTNESS_ROOT = MICROSERVICE_ROOT / "hotel-correctness"
HOTEL_CHECKER = (
    PROJECT_ROOT / "resources" / "evaluators" / "microservice" / "hotelcorrectness" / "check.py"
)


def _deathstar_bundle(task_name: str) -> InputBundle:
    return load_project_task(DEATHSTAR_LAYOUT, DEATHSTAR_TASKS[task_name])


def _adjacent_pairs(command: tuple[str, ...]) -> set[tuple[str, str]]:
    return set(pairwise(command))


def _load_hotel_checker(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    checker = HOTEL_CHECKER
    monkeypatch.syspath_prepend(str(PROJECT_ROOT / "libs" / "vs-correctness" / "src"))
    monkeypatch.syspath_prepend(str(checker.parent))
    spec = importlib.util.spec_from_file_location("hotel_accuracy_checker", checker)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_legacy_microservice_scenario_uses_source_evaluator() -> None:
    bundle = load_input_bundle(MICROSERVICE_ROOT / "train-ticket")

    assert bundle.evaluator_path == PROJECT_ROOT / "resources" / "evaluators" / "microservice"
    assert bundle.benchmark_command[:5] == (
        "go",
        "-C",
        "_evaluator/microservice",
        "run",
        "./cmd/servicebench",
    )
    assert bundle.benchmark_result is None
    assert bundle.benchmark_result_protocol == 2
    assert ("--seed", "random") in _adjacent_pairs(bundle.benchmark_command)
    assert ("--fixture-seed", "random") in _adjacent_pairs(bundle.benchmark_command)


@pytest.mark.parametrize(
    "task_name",
    ["hotel-reservation", "social-network-read-timeline"],
)
def test_deathstar_tasks_use_packaged_evaluator(task_name: str) -> None:
    bundle = _deathstar_bundle(task_name)

    assert bundle.root == DEATHSTAR_ROOT.resolve()
    assert bundle.task_name == task_name
    assert bundle.evaluator_path is None
    assert bundle.evaluator_package_digest is not None
    assert bundle.benchmark_command[:5] == (
        "go",
        "-C",
        str(PROJECT_ROOT / "resources" / "evaluators" / "microservice"),
        "run",
        "./cmd/servicebench",
    )
    assert bundle.benchmark_result is not None
    assert bundle.benchmark_result.json_argument == "--output-json"
    assert bundle.benchmark_result.metric == "primary_value"


def test_microservice_scenarios_are_discovered() -> None:
    assert set(DEATHSTAR_TASKS) == {
        "hotel-reservation",
        "social-network-read-timeline",
    }
    assert {path.name for path in LEGACY_SCENARIOS} == {"train-ticket"}


def test_train_ticket_accuracy_uses_source_evaluator() -> None:
    bundle = load_input_bundle(MICROSERVICE_ROOT / "train-ticket")

    assert bundle.accuracy_command[:5] == (
        "go",
        "-C",
        "_evaluator/microservice",
        "run",
        "./cmd/servicebench",
    )
    assert bundle.accuracy_command[5:7] == ("--mode", "accuracy")


def test_hotel_accuracy_and_benchmark_preserve_randomized_stateful_workload() -> None:
    bundle = load_input_bundle(HOTEL_CORRECTNESS_ROOT)
    accuracy_pairs = _adjacent_pairs(bundle.accuracy_command)
    benchmark_pairs = _adjacent_pairs(bundle.benchmark_command)
    assert bundle.accuracy_command[:2] == (
        "${PYTHON}",
        str(HOTEL_CHECKER),
    )
    assert ("--seed", "148622") in accuracy_pairs
    assert all("${PACKAGE_ROOT}" not in part for part in bundle.accuracy_command)
    assert (
        "--candidate-dir",
        f"{PROJECT_ROOT_TOKEN}/deathstarbench/hotelReservation",
    ) in accuracy_pairs
    assert ("--seed", "random") in benchmark_pairs
    assert ("--fixture-seed", "random") in benchmark_pairs
    assert bundle.benchmark_command[5] == "trace"
    assert (
        "--workload",
        str(HOTEL_CHECKER.with_name("workload.toml")),
    ) in benchmark_pairs
    assert ("--telemetry-output", str(HOTEL_TEMP_ROOT / "telemetry.json")) in benchmark_pairs
    assert ("--trace-graph-json", str(HOTEL_TEMP_ROOT / "trace-graph.json")) in benchmark_pairs
    assert ("--telemetry-timeout", "60") in benchmark_pairs

    run_command = bundle.benchmark_command[bundle.benchmark_command.index("--run-command-json") + 1]
    run_argv = json.loads(run_command)
    assert run_argv[:2] == ["sh", "-c"]
    assert 'go -C "$1" run ./cmd/otelinject' in run_argv[2]
    assert run_argv[4] == str(PROJECT_ROOT / "resources/evaluators/microservice")
    assert run_argv[5] == (
        f"{PROJECT_ROOT_TOKEN}/deathstarbench/hotelReservation/docker-compose.yml"
    )
    assert run_argv[6] == str(HOTEL_CHECKER.with_name("telemetry.toml"))
    assert bundle.manifest.workspace is not None
    assert bundle.manifest.workspace.sources[0].commit == (
        "867806e575e1f7fb24437ae969910ddb17a76121"
    )
    assert bundle.manifest.workspace.sources[0].dest == "deathstarbench"


def test_hotel_accuracy_wrapper_resolves_its_declared_evaluator_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The accuracy command resolves to evaluator-owned code, not candidate files."""
    bundle = load_input_bundle(HOTEL_CORRECTNESS_ROOT)
    _load_hotel_checker(monkeypatch)

    assert bundle.evaluator_package_root is not None
    assert bundle.accuracy_command[:2] == ("${PYTHON}", str(HOTEL_CHECKER))
    assert str(bundle.evaluator_package_root) in bundle.accuracy_command[1]


def test_hotel_accuracy_entrypoint_runs_from_unrelated_directory(tmp_path: Path) -> None:
    environment = {
        "PATH": os.defpath,
        "PYTHONPATH": str(PROJECT_ROOT / "libs" / "vs-correctness" / "src"),
    }

    result = subprocess.run(  # noqa: S603
        [sys.executable, str(HOTEL_CHECKER), "--help"],
        cwd=tmp_path,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "--compose-project-name" in result.stdout


def test_hotel_accuracy_waits_for_every_dependency_before_verification(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    checker = _load_hotel_checker(monkeypatch)
    probes = {
        "auth": "http://localhost:5000/user?username=Cornell_30&password=0000000000",
        "recommendations": (
            "http://localhost:5000/recommendations?require=rate&lat=37.7867&lon=-122.4112"
        ),
        "hotels": (
            "http://localhost:5000/hotels?inDate=2300-01-01&outDate=2300-01-02"
            "&lat=37.7867&lon=-122.4112"
        ),
    }
    calls: list[object] = []

    class Response:
        def __init__(self, status: int) -> None:
            self.status = status

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

    def urlopen(url: object, *, timeout: float) -> Response:
        assert timeout > 0
        calls.append(url)
        status = 500 if url == probes["auth"] and calls.count(url) == 1 else 200
        return Response(status)

    monkeypatch.setattr(checker.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(checker.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        checker, "_compose_services", lambda _path, _compose: ("frontend", "jaeger")
    )

    def run(_command: list[str], *, cwd: Path) -> None:
        assert cwd == tmp_path / "candidate" / "hotelReservation"

    monkeypatch.setattr(checker, "_run", run)
    monkeypatch.setattr(checker, "write_report", lambda _report, _path: None)
    monkeypatch.setattr(checker, "gate_exit_code", lambda _report: 0)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check.py",
            "--candidate-dir",
            str(tmp_path / "candidate" / "hotelReservation"),
            "--seed",
            "42",
            "--report",
            str(tmp_path / "report.json"),
        ],
    )

    class Verifier:
        def __init__(
            self,
            *_args: object,
            reset: Callable[[Environment], None],
            **_kwargs: object,
        ) -> None:
            self.reset = reset

        def verify(self, *_args: object, **_kwargs: object) -> object:
            self.reset(Environment(name="candidate", base_url="http://candidate"))
            assert calls == [*probes.values()] * 3
            return object()

    class CompletedProcess:
        returncode = 0

    monkeypatch.setattr(checker, "Verifier", Verifier)
    monkeypatch.setattr(checker.subprocess, "run", lambda *_args, **_kwargs: CompletedProcess())

    assert checker.main() == 0


def test_hotel_readiness_timeout_names_each_failed_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = _load_hotel_checker(monkeypatch)
    probes = {
        "auth": "http://localhost:5000/user",
        "recommendations": "http://localhost:5000/recommendations",
        "hotels": "http://localhost:5000/hotels",
    }
    now = 0.0
    connection_refused = "connection refused"

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    def unavailable(_url: str, *, timeout: float) -> None:
        assert 0 < timeout <= 2
        raise OSError(connection_refused)

    monkeypatch.setattr(checker.time, "monotonic", lambda: now)
    monkeypatch.setattr(checker.time, "sleep", sleep)
    monkeypatch.setattr(checker.urllib.request, "urlopen", unavailable)

    with pytest.raises(TimeoutError) as error:
        checker._wait_ready(probes, 2)  # noqa: SLF001

    message = str(error.value)
    assert all(name in message for name in probes)
    assert "connection refused" in message


@pytest.mark.parametrize(
    ("artifacts", "expected"),
    [
        ({"compose_services": ["frontend", "jaeger"]}, Verdict.PASS),
        ({"compose_services": ["frontend"]}, Verdict.FAIL),
        ({}, Verdict.INCONCLUSIVE),
    ],
    ids=("jaeger-present", "jaeger-missing", "observation-missing"),
)
def test_hotel_oracle_checks_observed_compose_topology(
    monkeypatch: pytest.MonkeyPatch,
    artifacts: dict[str, JsonValue],
    expected: Verdict,
) -> None:
    checker = _load_hotel_checker(monkeypatch)
    oracle = checker.SUITE.oracle
    environment = Environment(name="candidate", base_url="http://candidate")
    observation = Observation(
        environment=environment,
        action_results=(),
        artifacts=artifacts,
    )
    decision = oracle.check(
        OracleContext(
            test_case=CorrectnessTestCase(id="topology", actions=()),
            candidate=observation,
        )
    )

    assert decision.verdict is expected


def test_hotel_executor_attaches_observed_compose_topology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = _load_hotel_checker(monkeypatch)
    environment = Environment(name="candidate", base_url="http://candidate")
    executor = checker.HotelExecutor(("frontend", "jaeger"), lambda: None)

    result = executor.execute(CorrectnessTestCase(id="topology", actions=()), environment)

    assert result.artifacts == {"compose_services": ["frontend", "jaeger"]}


def test_hotel_generator_is_deterministic_and_covers_every_required_property(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = _load_hotel_checker(monkeypatch)
    context = GenerationContext(seed=148622, cases=8)

    first = checker.SUITE.generator.generate(context)
    second = checker.SUITE.generator.generate(GenerationContext(seed=148622, cases=8))
    serialized = [case.model_dump_json() for case in first]
    assert serialized == [case.model_dump_json() for case in second]
    assert serialized != [
        case.model_dump_json()
        for case in checker.SUITE.generator.generate(GenerationContext(seed=148623, cases=8))
    ]
    assert checker.SUITE.oracle.properties == checker.REQUIRED_PROPERTIES
    assert all("metadata" not in case.model_dump() for case in first)
    assert "expectation" not in "".join(serialized)
    assert len(first) == 8
    assert [len(case.actions) for case in first[:4]] == [178, 521, 15, 4]


def test_hotel_cases_reject_forged_expectation_metadata() -> None:
    with pytest.raises(ValueError, match="metadata"):
        CorrectnessTestCase.model_validate(
            {"id": "forged", "actions": [], "metadata": {"expectations": [{"status": 200}]}}
        )


def test_hotel_oracle_derives_expectation_from_mutated_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = _load_hotel_checker(monkeypatch)
    environment = Environment(name="candidate", base_url="http://candidate")
    valid = HTTPAction(
        method="get",
        path="/user",
        query={"username": "Cornell_30", "password": "0000000000"},
    )
    case = CorrectnessTestCase(id="arbitrary-report-label", actions=(valid,))
    success = Observation(
        environment=environment,
        action_results=(
            ActionResult(
                phase="actions",
                index=0,
                status=200,
                body=json.dumps({"message": "Login successfully!"}),
            ),
        ),
        artifacts={"compose_services": ["frontend", "jaeger"]},
    )
    assert (
        checker.SUITE.oracle.check(OracleContext(test_case=case, candidate=success)).verdict
        is Verdict.PASS
    )

    invalid = valid.model_copy(update={"query": {**valid.query, "password": "wrong"}})
    mutated_case = case.model_copy(update={"actions": (invalid,)})
    assert (
        checker.SUITE.oracle.check(OracleContext(test_case=mutated_case, candidate=success)).verdict
        is Verdict.FAIL
    )
    failure = success.model_copy(
        update={
            "action_results": (
                success.action_results[0].model_copy(
                    update={
                        "body": json.dumps(
                            {"message": "Failed. Please check your username and password. "}
                        )
                    }
                ),
            )
        }
    )
    assert (
        checker.SUITE.oracle.check(OracleContext(test_case=mutated_case, candidate=failure)).verdict
        is Verdict.PASS
    )


@pytest.mark.parametrize(("socket_mode", "expected_status"), [("same", 204), ("different", 412)])
def test_hotel_persistent_http_probe_observes_socket_reuse(
    monkeypatch: pytest.MonkeyPatch,
    socket_mode: str,
    expected_status: int,
) -> None:
    checker = _load_hotel_checker(monkeypatch)

    class Response:
        status = 200

        @staticmethod
        def read() -> bytes:
            return b'{"message":"Login successfully!"}'

    class Connection:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            self.first_socket = object()
            self.sock = self.first_socket
            self.requests = 0

        def request(self, *_args: object) -> None:
            self.requests += 1
            if self.requests == 2 and socket_mode == "different":
                self.sock = object()

        def getresponse(self) -> Response:
            return Response()

        def close(self) -> None:
            pass

    monkeypatch.setattr(checker.http.client, "HTTPConnection", Connection)
    executor = checker.HotelExecutor(("frontend", "jaeger"), lambda: None)
    result = executor.execute(
        CorrectnessTestCase(id="persistent", actions=(CustomAction(name="persistent-http"),)),
        Environment(name="candidate", base_url="http://candidate"),
    )

    assert result.action_results[0].status == expected_status
    assert result.action_results[0].error is None


def test_hotel_oracle_rejects_a_real_authentication_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = _load_hotel_checker(monkeypatch)
    case = checker.SUITE.generator.generate(GenerationContext(seed=148622, cases=4))[1]
    results: list[ActionResult] = []
    for index, action in enumerate(case.actions):
        if isinstance(action, CustomAction):
            status, body = 204, ""
        elif checker.SUITE.oracle._classify_http(action) is None:  # noqa: SLF001
            status, body = 400, ""
        else:
            status = 200
            query = action.query
            valid = (
                not query.get("password", "").endswith("-wrong")
                and query["username"] != "Cornell_missing"
            )
            message = (
                "Login successfully!"
                if valid
                else "Failed. Please check your username and password. "
            )
            body = json.dumps({"message": message})
        results.append(ActionResult(phase="actions", index=index, status=status, body=body))
    environment = Environment(name="candidate", base_url="http://candidate")
    passing = Observation(
        environment=environment,
        action_results=tuple(results),
        artifacts={"compose_services": ["frontend", "jaeger"]},
    )
    assert (
        checker.SUITE.oracle.check(OracleContext(test_case=case, candidate=passing)).verdict
        is Verdict.PASS
    )

    mutated = results.copy()
    mutated[0] = mutated[0].model_copy(
        update={"body": json.dumps({"message": "accepted any credentials"})}
    )
    failing = passing.model_copy(update={"action_results": tuple(mutated)})
    assert (
        checker.SUITE.oracle.check(OracleContext(test_case=case, candidate=failing)).verdict
        is Verdict.FAIL
    )

    persistent_index = next(
        index for index, action in enumerate(case.actions) if isinstance(action, CustomAction)
    )
    non_reused = results.copy()
    non_reused[persistent_index] = non_reused[persistent_index].model_copy(
        update={"status": 412, "body": "frontend did not reuse the HTTP connection"}
    )
    assert (
        checker.SUITE.oracle.check(
            OracleContext(
                test_case=case,
                candidate=passing.model_copy(update={"action_results": tuple(non_reused)}),
            )
        ).verdict
        is Verdict.FAIL
    )

    inconclusive = passing.model_copy(
        update={
            "action_results": (
                results[0].model_copy(update={"error": "connection refused"}),
                *results[1:],
            )
        }
    )
    assert (
        checker.SUITE.oracle.check(OracleContext(test_case=case, candidate=inconclusive)).verdict
        is Verdict.INCONCLUSIVE
    )


def test_hotel_reservation_negative_cases_isolate_each_malformed_field(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = _load_hotel_checker(monkeypatch)
    case = checker.SUITE.generator.generate(GenerationContext(seed=148622, cases=4))[1]
    malformed = [
        action
        for action in case.actions
        if getattr(action, "path", None) == "/reservation"
        and checker.SUITE.oracle._classify_http(action) is None  # noqa: SLF001
    ]
    base_fields = {
        "hotelId",
        "inDate",
        "outDate",
        "customerName",
        "username",
        "password",
        "number",
    }

    assert len(malformed) == 5
    assert set(malformed[0].query) == base_fields
    assert malformed[0].query["inDate"] == "not-a-date"
    assert {frozenset(base_fields - set(action.query)) for action in malformed[1:]} == {
        frozenset({field}) for field in ("inDate", "hotelId", "customerName", "password")
    }


def test_hotel_restart_wait_requires_every_endpoint_to_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = _load_hotel_checker(monkeypatch)
    now = 0.0

    class Response:
        status = 200

        def __enter__(self) -> "Response":
            return self

        def __exit__(self, *_args: object) -> None:
            pass

    def sleep(seconds: float) -> None:
        nonlocal now
        now += seconds

    monkeypatch.setattr(checker.time, "monotonic", lambda: now)
    monkeypatch.setattr(checker.time, "sleep", sleep)
    monkeypatch.setattr(checker.urllib.request, "urlopen", lambda *_args, **_kwargs: Response())

    with pytest.raises(TimeoutError, match="remained available"):
        checker._wait_stopped({"frontend": "http://candidate/user"}, 1)  # noqa: SLF001


def test_hotel_accuracy_cleans_up_when_readiness_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    checker = _load_hotel_checker(monkeypatch)
    commands: list[list[str]] = []
    project_root = tmp_path / "candidate"
    report = tmp_path / "report.json"

    def record_run(command: list[str], *, cwd: Path) -> None:
        assert cwd == project_root / "hotelReservation"
        commands.append(command)

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "check.py",
            "--candidate-dir",
            str(project_root / "hotelReservation"),
            "--seed",
            "42",
            "--report",
            str(report),
        ],
    )
    monkeypatch.setattr(
        checker, "_compose_services", lambda _path, _compose: ("frontend", "jaeger")
    )
    monkeypatch.setattr(checker, "_run", record_run)
    monkeypatch.setattr(
        checker,
        "_wait_ready",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("not ready")),
    )

    class Verifier:
        def __init__(
            self,
            *_args: object,
            reset: Callable[[Environment], None],
            **_kwargs: object,
        ) -> None:
            self.reset = reset

        def verify(self, *_args: object, **_kwargs: object) -> object:
            self.reset(Environment(name="candidate", base_url="http://candidate"))
            raise AssertionError("unreachable")

    monkeypatch.setattr(checker, "Verifier", Verifier)

    with pytest.raises(TimeoutError, match="not ready"):
        checker.main()

    assert commands == [
        ["docker", "compose", "down", "-v", "--remove-orphans"],
        ["docker", "compose", "down", "-v", "--remove-orphans"],
        ["docker", "compose", "up", "-d", "--build"],
        ["docker", "compose", "down", "-v", "--remove-orphans"],
    ]


@pytest.mark.parametrize(
    "scenario_path",
    [
        MICROSERVICE_ROOT / "train-ticket",
        DEATHSTAR_TASKS["hotel-reservation"].path,
        DEATHSTAR_TASKS["social-network-read-timeline"].path,
    ],
    ids=("train-ticket", "hotel-reservation", "social-network-read-timeline"),
)
def test_microservice_scenario_has_no_embedded_legacy_generator(
    scenario_path: Path,
) -> None:
    benchmark_dir = scenario_path / "benchmark"
    legacy_sources = sorted(
        path.relative_to(scenario_path)
        for path in benchmark_dir.iterdir()
        if path.name == "benchmark" or path.suffix in {".cpp", ".py"}
    )

    assert legacy_sources == []


def test_social_network_workload_uses_stateful_semantic_operation() -> None:
    workload_path = (
        DEATHSTAR_TASKS["social-network-read-timeline"].path / "benchmark" / "workload.toml"
    )
    with workload_path.open("rb") as file:
        workload = tomllib.load(file)

    operations = {operation["name"]: operation for operation in workload["operations"]}
    assert set(operations) == {
        "user_timeline_read",
        "home_timeline_read",
        "compose_user_timeline",
    }
    assert operations["compose_user_timeline"]["tags"] == [
        "write",
        "read-your-write",
    ]
    assert {
        capture["header"] for capture in operations["compose_user_timeline"]["capture_headers"]
    } == {
        "X-Compose-Thrift-Ms",
        "X-UserTimeline-Thrift-Ms",
        "X-HomeTimeline-Thrift-Ms",
    }
    assert workload["load"]["seed"] == 42
    assert workload["load"]["fixture_seed"] == 42
    assert workload["constraints"]["min_operations_per_type"] == 1


def test_hotel_workload_preserves_canonical_mix_and_stateful_gate() -> None:
    workload_path = DEATHSTAR_TASKS["hotel-reservation"].path / "benchmark" / "workload.toml"
    with workload_path.open("rb") as file:
        workload = tomllib.load(file)

    operations = {operation["name"]: operation for operation in workload["operations"]}
    assert {name: operation["weight"] for name, operation in operations.items()} == {
        "search_hotels": 600,
        "recommend_distance": 130,
        "recommend_rate": 130,
        "recommend_price": 130,
        "login_valid": 3,
        "login_invalid": 2,
        "reserve_capacity": 5,
    }
    assert operations["reserve_capacity"]["tags"] == ["write", "read-your-write"]
    assert workload["load"]["model"] == "closed_loop"
    assert workload["load"]["repetitions"] == 3
    assert workload["profiles"]["quick"]["repetitions"] == 1
    assert workload["constraints"] == {
        "min_success_rate": 1.0,
        "max_error_rate": 0.0,
        "min_operations_per_type": 1,
    }
