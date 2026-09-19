from __future__ import annotations

import json
import signal
import socket
import subprocess
import sys
import threading
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar, cast

if TYPE_CHECKING:
    from collections.abc import Callable

import pytest
import yaml
from pydantic import ValidationError

EVALUATOR_ROOT = Path(__file__).parents[2] / "resources/evaluators/microservice"
EXAMPLES_ROOT = Path(__file__).parents[2] / "examples/microservices"
SOCIAL_CONFIG = EXAMPLES_ROOT / "social-network-kubernetes/.vibesys/tasks/kubernetes/runtime.yaml"
TRAIN_CONFIG = EXAMPLES_ROOT / "train-ticket-kubernetes/.vibesys/tasks/kubernetes/runtime.yaml"
sys.path.insert(0, str(EVALUATOR_ROOT))

from kubernetes_runtime import (  # noqa: E402
    HTTPProbe,
    KubernetesConfig,
    KubernetesLifecycle,
    ServiceForward,
)
from kubernetes_runtime import cli as runtime_cli  # noqa: E402
from kubernetes_runtime.control import LifecycleControlServer, request_action  # noqa: E402


class _Process:
    def __init__(self) -> None:
        self.terminated = False

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return 0

    def kill(self) -> None:
        self.terminated = True

    def poll(self) -> int | None:
        return None if not self.terminated else 0


class _Runner:
    def __init__(self, *, fail_rollout: bool = False, malformed_create: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.timeouts: list[float] = []
        self.token = ""
        self.fail_rollout = fail_rollout
        self.malformed_create = malformed_create
        self.fail_delete_once = False

    def __call__(
        self,
        command: list[str],
        *,
        cwd: Path,
        input_text: str | None = None,
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        del cwd
        self.calls.append(command)
        self.timeouts.append(timeout_seconds)
        if "create" in command:
            namespace = yaml.safe_load(input_text or "")
            self.token = namespace["metadata"]["labels"]["vibesys.dev/evaluator-owned"]
            output = "not-json" if self.malformed_create else '{"metadata":{"uid":"uid-1"}}'
            return subprocess.CompletedProcess(command, 0, output, "")
        if "get" in command and "namespace" in command:
            output = f'{{"metadata":{{"uid":"uid-1","labels":{{"vibesys.dev/evaluator-owned":"{self.token}"}}}}}}'
            return subprocess.CompletedProcess(command, 0, output, "")
        if "get" in command and any(part.startswith("deployment/") for part in command):
            return subprocess.CompletedProcess(command, 0, "2", "")
        if "get" in command and "service" in command:
            return subprocess.CompletedProcess(command, 0, '{"items":[]}', "")
        if "rollout" in command and self.fail_rollout:
            self.fail_rollout = False
            raise RuntimeError("rollout failed")  # noqa: TRY003
        if "delete" in command and "namespace" in command and self.fail_delete_once:
            self.fail_delete_once = False
            raise RuntimeError("transient delete")  # noqa: TRY003
        return subprocess.CompletedProcess(command, 0, "", "")


def _config(manifest: Path) -> KubernetesConfig:
    return KubernetesConfig(
        context="kind-test",
        namespace_prefix="vibesys-test",
        manifests=(manifest,),
        rollouts=("deployment/frontend",),
        forwards=(
            ServiceForward(
                name="frontend", resource="service/frontend", remote_port=5000, local_port=15000
            ),
            ServiceForward(
                name="users", resource="service/users", remote_port=8080, local_port=18080
            ),
        ),
        primary_endpoint="frontend",
        http_probes=(HTTPProbe(endpoint="frontend", path="/health"),),
    )


def test_manifest_is_config_relative_and_namespace_scoped(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    config_dir = tmp_path / "config"
    candidate.mkdir()
    config_dir.mkdir()
    manifest = config_dir / "deployment.yaml"
    manifest.write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: frontend\n"
        "spec:\n  selector:\n    matchLabels:\n      app: frontend\n"
        "  template:\n    metadata:\n      labels:\n        app: frontend\n"
        "    spec:\n      containers: []\n",
        encoding="utf-8",
    )
    lifecycle = KubernetesLifecycle(
        _config(Path("deployment.yaml")), candidate, config_dir=config_dir
    )

    documents = list(yaml.safe_load_all(lifecycle._render_manifests("owned-ns")))  # noqa: SLF001

    assert documents[0]["metadata"]["namespace"] == "owned-ns"
    assert lifecycle.base_url == "http://127.0.0.1:15000"
    assert lifecycle.endpoints["users"] == "http://127.0.0.1:18080"


def test_cluster_scoped_manifest_rejected_before_runner(tmp_path: Path) -> None:
    manifest = tmp_path / "bad.yaml"
    manifest.write_text(
        "apiVersion: v1\nkind: Namespace\nmetadata:\n  name: bad\n", encoding="utf-8"
    )
    calls: list[list[str]] = []

    def runner(
        command: list[str],
        *,
        cwd: Path,
        input_text: str | None = None,
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        del cwd, input_text, timeout_seconds
        calls.append(command)
        pytest.fail("runner must not be called")

    lifecycle = KubernetesLifecycle(
        _config(Path("bad.yaml")), tmp_path, config_dir=tmp_path, runner=runner
    )

    with pytest.raises(ValueError, match="cluster-scoped"):
        lifecycle.start()
    assert calls == []


def test_probe_must_reference_a_forward() -> None:
    with pytest.raises(ValidationError, match="configured forwards"):
        _config(Path("manifest.yaml")).model_copy(
            update={"http_probes": (HTTPProbe(endpoint="missing", path="/"),)}
        ).model_validate(
            {
                **_config(Path("manifest.yaml")).model_dump(),
                "http_probes": [{"endpoint": "missing", "path": "/"}],
            }
        )


class _HTTPResponse(BytesIO):
    status = 200


def test_http_probe_waits_for_expected_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(Path("manifest.yaml")).model_copy(
        update={
            "http_probes": (
                HTTPProbe(
                    endpoint="frontend",
                    path="/catalog",
                    json_contains={"data": [{"id": "seed-final"}]},
                ),
            )
        }
    )
    lifecycle = KubernetesLifecycle(config, tmp_path, config_dir=tmp_path)
    responses = iter(
        [
            _HTTPResponse(b'{"data":[{"id":"seed-first"}]}'),
            _HTTPResponse(b'{"data":[{"id":"seed-first"},{"id":"seed-final"}]}'),
        ]
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    lifecycle._wait_http()  # noqa: SLF001


def test_http_probe_json_timeout_reports_expected_subset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(Path("manifest.yaml")).model_copy(
        update={
            "timeout_seconds": 1,
            "http_probes": (
                HTTPProbe(endpoint="frontend", path="/catalog", json_contains={"ready": True}),
            ),
        }
    )
    lifecycle = KubernetesLifecycle(config, tmp_path, config_dir=tmp_path)
    times = iter([0.0, 0.0, 2.0])
    monkeypatch.setattr("time.monotonic", lambda: next(times))
    monkeypatch.setattr(
        "urllib.request.urlopen", lambda *_args, **_kwargs: _HTTPResponse(b'{"ready":1}')
    )
    monkeypatch.setattr("time.sleep", lambda _seconds: None)

    with pytest.raises(TimeoutError, match=r"JSON does not contain \{'ready': True\}"):
        lifecycle._wait_http()  # noqa: SLF001


def test_lifecycle_start_restart_reset_and_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: frontend\n", encoding="utf-8"
    )
    config = _config(Path("manifest.yaml")).model_copy(
        update={"restart_deployments": ({"name": "frontend", "pod_selector": "app=frontend"},)}
    )
    config = KubernetesConfig.model_validate(config.model_dump())
    runner = _Runner()
    processes: list[_Process] = []

    def popen(*_args: object, **_kwargs: object) -> _Process:
        process = _Process()
        processes.append(process)
        return process

    monkeypatch.setattr(KubernetesLifecycle, "_wait_http", lambda _self: None)
    lifecycle = KubernetesLifecycle(
        config, tmp_path, config_dir=tmp_path, runner=runner, popen=popen
    )
    lifecycle.start()
    first_namespace = lifecycle.namespace
    lifecycle.start_stopped()
    assert len(processes) == 2
    assert not any("scale deployment/frontend" in " ".join(call) for call in runner.calls)
    lifecycle.stop()

    flattened = [" ".join(call) for call in runner.calls]
    assert any("scale deployment/frontend --replicas=0" in call for call in flattened)
    assert any("wait --for=delete pod --selector app=frontend" in call for call in flattened)
    assert processes[0].terminated
    lifecycle.start_stopped()
    flattened = [" ".join(call) for call in runner.calls]
    assert any("scale deployment/frontend --replicas=2" in call for call in flattened)
    assert len(processes) == 4

    lifecycle.reset()
    assert lifecycle.namespace != first_namespace
    lifecycle.close()
    assert all(process.terminated for process in processes)
    assert (
        sum(
            "delete namespace" in call
            for call in flattened + [" ".join(c) for c in runner.calls[len(flattened) :]]
        )
        >= 1
    )


def test_rollout_failure_cleans_owned_namespace(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: frontend\n", encoding="utf-8"
    )
    runner = _Runner(fail_rollout=True)
    lifecycle = KubernetesLifecycle(
        _config(Path("manifest.yaml")), tmp_path, config_dir=tmp_path, runner=runner
    )

    with pytest.raises(RuntimeError, match="rollout failed"):
        lifecycle.start()
    assert any("delete namespace" in " ".join(call) for call in runner.calls)


def test_malformed_namespace_create_output_recovers_and_cleans(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: frontend\n", encoding="utf-8"
    )
    runner = _Runner(malformed_create=True)
    lifecycle = KubernetesLifecycle(
        _config(Path("manifest.yaml")), tmp_path, config_dir=tmp_path, runner=runner
    )

    with pytest.raises(RuntimeError, match="response has no UID"):
        lifecycle.start()
    assert any("delete namespace" in " ".join(call) for call in runner.calls)


def test_foreign_namespace_is_never_deleted(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: frontend\n", encoding="utf-8"
    )
    runner = _Runner()
    lifecycle = KubernetesLifecycle(
        _config(Path("manifest.yaml")), tmp_path, config_dir=tmp_path, runner=runner
    )
    lifecycle._namespace = "foreign"  # noqa: SLF001
    lifecycle._namespace_uid = "uid-1"  # noqa: SLF001
    lifecycle._ownership_token = "ownership-expected"  # noqa: S105, SLF001
    runner.token = "ownership-different"  # noqa: S105

    with pytest.raises(RuntimeError, match="ownership changed"):
        lifecycle.close()
    assert not any("delete namespace" in " ".join(call) for call in runner.calls)


def test_foreign_namespace_is_never_scaled(tmp_path: Path) -> None:
    runner = _Runner()
    config = _config(Path("manifest.yaml")).model_copy(
        update={"restart_deployments": ({"name": "frontend", "pod_selector": "app=frontend"},)}
    )
    lifecycle = KubernetesLifecycle(
        KubernetesConfig.model_validate(config.model_dump()), tmp_path, runner=runner
    )
    lifecycle._namespace = "foreign"  # noqa: SLF001
    lifecycle._namespace_uid = "uid-1"  # noqa: SLF001
    lifecycle._ownership_token = "ownership-expected"  # noqa: S105, SLF001
    runner.token = "ownership-different"  # noqa: S105

    with pytest.raises(RuntimeError, match="ownership changed"):
        lifecycle.stop()
    assert not any("scale deployment" in " ".join(call) for call in runner.calls)


def test_delete_failure_retains_ownership_for_retry(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "apiVersion: v1\nkind: Service\nmetadata:\n  name: frontend\n", encoding="utf-8"
    )
    runner = _Runner()
    lifecycle = KubernetesLifecycle(
        _config(Path("manifest.yaml")), tmp_path, config_dir=tmp_path, runner=runner
    )
    lifecycle._namespace = "owned"  # noqa: SLF001
    lifecycle._namespace_uid = "uid-1"  # noqa: SLF001
    lifecycle._ownership_token = "ownership-token"  # noqa: S105, SLF001
    runner.token = "ownership-token"  # noqa: S105
    runner.fail_delete_once = True

    with pytest.raises(RuntimeError, match="transient delete"):
        lifecycle.close()
    assert lifecycle.namespace == "owned"
    lifecycle.close()
    with pytest.raises(RuntimeError, match="not started"):
        _ = lifecycle.namespace


def test_cli_propagates_child_failure_and_always_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "runtime.yaml"
    config.write_text("{}", encoding="utf-8")
    closed: list[bool] = []

    class Lifecycle:
        base_url = "http://127.0.0.1:15000"
        endpoints: ClassVar[dict[str, str]] = {"frontend": base_url}

        def start(self) -> None:
            return None

        def close(self) -> None:
            closed.append(True)

        def stop(self) -> None:
            return None

        def start_stopped(self) -> None:
            return None

    class Child:
        pid = 123

        def wait(self, timeout: float | None = None) -> int:
            del timeout
            return 7

        def poll(self) -> int:
            return 7

    monkeypatch.setattr(runtime_cli, "load_config", lambda _path: object())
    monkeypatch.setattr(runtime_cli, "KubernetesLifecycle", lambda *_args, **_kwargs: Lifecycle())
    monkeypatch.setattr(runtime_cli.subprocess, "Popen", lambda *_args, **_kwargs: Child())

    result = runtime_cli.main(
        ["--config", str(config), "--candidate-dir", str(tmp_path), "--", "false"]
    )

    assert result == 7
    assert closed == [True]


def test_control_server_serializes_stop_and_start(tmp_path: Path) -> None:
    socket_path = tmp_path / "control.sock"
    actions: list[str] = []

    with LifecycleControlServer(
        socket_path,
        {"stop": lambda: actions.append("stop"), "start": lambda: actions.append("start")},
    ):
        request_action(socket_path, "stop")
        request_action(socket_path, "start")

    assert actions == ["stop", "start"]
    assert not socket_path.exists()


def test_control_server_reports_action_failure(tmp_path: Path) -> None:
    socket_path = tmp_path / "control.sock"

    class ScaleError(RuntimeError):
        def __init__(self) -> None:
            super().__init__("scale failed")

    def fail() -> None:
        raise ScaleError

    with (
        LifecycleControlServer(socket_path, {"stop": fail}),
        pytest.raises(RuntimeError, match="scale failed"),
    ):
        request_action(socket_path, "stop")


def test_control_server_ignores_peer_closed_after_completed_action(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    socket_path = tmp_path / "control.sock"
    action_started = threading.Event()
    finish_action = threading.Event()

    def action() -> None:
        action_started.set()
        finish_action.wait(timeout=1)

    with LifecycleControlServer(socket_path, {"stop": action}):
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(str(socket_path))
        client.sendall(b'{"action":"stop"}\n')
        assert action_started.wait(timeout=1)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\1\0\0\0\0\0\0\0")
        client.close()
        finish_action.set()

    assert "BrokenPipeError" not in capsys.readouterr().err


def test_control_server_shutdown_is_bounded_for_incomplete_request(tmp_path: Path) -> None:
    socket_path = tmp_path / "control.sock"
    server = LifecycleControlServer(socket_path, {})
    server.__enter__()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(socket_path))
    client.sendall(b'{"action":"stop"')
    stopped = threading.Event()
    shutdown = threading.Thread(target=lambda: (server.__exit__(), stopped.set()), daemon=True)
    shutdown.start()

    completed_without_peer_close = stopped.wait(timeout=2)
    client.close()
    shutdown.join(timeout=2)

    assert completed_without_peer_close


def test_cli_renders_managed_lifecycle_control_commands(tmp_path: Path) -> None:
    lifecycle = cast(
        "KubernetesLifecycle",
        type("Lifecycle", (), {"base_url": "http://frontend", "endpoints": {}})(),
    )
    socket_path = tmp_path / "control.sock"

    rendered = runtime_cli._render_command(  # noqa: SLF001
        [
            "checker",
            "--stop-command-json",
            "${KUBERNETES_STOP_COMMAND_JSON}",
            "--run-command-json",
            "${KUBERNETES_START_COMMAND_JSON}",
            "--cleanup-command-json",
            "${KUBERNETES_CLEANUP_COMMAND_JSON}",
        ],
        lifecycle,
        socket_path,
    )

    stop = json.loads(rendered[2])
    start = json.loads(rendered[4])
    cleanup = json.loads(rendered[6])
    assert stop[-3:] == ["--control", str(socket_path), "stop"]
    assert start[-3:] == ["--control", str(socket_path), "start"]
    assert cleanup[-3:] == ["--control", str(socket_path), "cleanup"]


def test_cli_signal_during_startup_closes_lifecycle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "runtime.yaml"
    config.write_text("{}", encoding="utf-8")
    handlers: dict[signal.Signals, object] = {}
    closed: list[bool] = []

    def register(sig: signal.Signals, handler: object) -> object:
        previous = handlers.get(sig, signal.SIG_DFL)
        handlers[sig] = handler
        return previous

    class Lifecycle:
        def start(self) -> None:
            handler = handlers[signal.SIGTERM]
            callback = cast("Callable[[int, object], object]", handler)
            callback(signal.SIGTERM, None)

        def close(self) -> None:
            closed.append(True)

    monkeypatch.setattr(runtime_cli, "load_config", lambda _path: object())
    monkeypatch.setattr(runtime_cli, "KubernetesLifecycle", lambda *_args, **_kwargs: Lifecycle())
    monkeypatch.setattr(runtime_cli.signal, "signal", register)

    result = runtime_cli.main(
        ["--config", str(config), "--candidate-dir", str(tmp_path), "--", "true"]
    )

    assert result == 130
    assert closed == [True]


def test_social_network_assets_build_and_override_candidate_services() -> None:
    config = runtime_cli.load_config(SOCIAL_CONFIG)
    lifecycle = KubernetesLifecycle(
        config,
        Path("examples/microservices/repositories"),
        config_dir=SOCIAL_CONFIG.parent,
    )

    rendered = lifecycle._render_manifests("social-validation")  # noqa: SLF001
    documents = list(yaml.safe_load_all(rendered))

    assert len(config.image_builds) == 1
    assert config.image_builds[0].context == Path("deathstarbench/socialNetwork")
    assert len(config.image_overrides) == 11
    assert all(override.image == "${IMAGE:candidate}" for override in config.image_overrides)
    assert "kind: Namespace" not in rendered
    assert rendered.count("namespace: social-validation") == 78
    mongodb_deployments = [
        document
        for document in documents
        if document["kind"] == "Deployment" and document["metadata"]["name"].endswith("-mongodb")
    ]
    assert len(mongodb_deployments) == 6
    for deployment in mongodb_deployments:
        resources = deployment["spec"]["template"]["spec"]["containers"][0]["resources"]
        assert resources["limits"]["memory"] == "512Mi"
        assert resources["requests"]["memory"] == "128Mi"
    nginx = next(
        document
        for document in documents
        if document["kind"] == "Deployment" and document["metadata"]["name"] == "nginx-thrift"
    )
    loader_resources = nginx["spec"]["template"]["spec"]["initContainers"][0]["resources"]
    assert loader_resources["limits"]["memory"] == "512Mi"
    assert loader_resources["requests"]["memory"] == "128Mi"
    nginx_resources = nginx["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert nginx_resources["limits"]["memory"] == "512Mi"
    assert nginx_resources["requests"]["memory"] == "128Mi"
    nginx_config = next(
        document
        for document in documents
        if document["kind"] == "ConfigMap" and document["metadata"]["name"] == "nginx-thrift"
    )
    assert "worker_processes  1;" in nginx_config["data"]["nginx.conf"]
    loader_command = nginx["spec"]["template"]["spec"]["initContainers"][0]["args"][1]
    assert "fetch --depth=1 origin 867806e575e1f7fb24437ae969910ddb17a76121" in loader_command
    deployments = {
        document["metadata"]["name"]: document
        for document in documents
        if document["kind"] == "Deployment"
    }
    for name, deployment in deployments.items():
        resources = deployment["spec"]["template"]["spec"]["containers"][0]["resources"]
        expected_limit = "100m" if name == "jaeger" else "1"
        assert resources["limits"]["cpu"] == expected_limit
        assert resources["requests"]["cpu"] == "100m"
    for override in config.image_overrides:
        candidate = deployments[override.resource.removeprefix("deployment/")]
        probe = candidate["spec"]["template"]["spec"]["containers"][0]["readinessProbe"]
        assert probe["tcpSocket"]["port"] == 9090
        assert probe["periodSeconds"] == 2


def test_train_ticket_assets_build_current_java_modules(tmp_path: Path) -> None:
    candidate = tmp_path / "workspace"
    source = candidate / "train-ticket"
    source.mkdir(parents=True)
    config = runtime_cli.load_config(TRAIN_CONFIG)
    lifecycle = KubernetesLifecycle(config, candidate, config_dir=TRAIN_CONFIG.parent)

    rendered = lifecycle._render_manifests("train-validation")  # noqa: SLF001
    dockerfile = (TRAIN_CONFIG.parent / "Dockerfile").read_text(encoding="utf-8")

    assert {build.build_args["MODULE"] for build in config.image_builds} == {
        "ts-config-service",
        "ts-station-service",
        "ts-train-service",
        "ts-travel-service",
        "ts-route-service",
        "ts-price-service",
    }
    assert all(build.context == Path("train-ticket") for build in config.image_builds)
    assert all(str(build.dockerfile).startswith("${CONFIG_DIR}/") for build in config.image_builds)
    assert "mvn -B" in dockerfile
    assert "COPY . ." in dockerfile
    assert "COPY target" not in dockerfile
    assert rendered.count("namespace: train-validation") == 24
    documents = list(yaml.safe_load_all(rendered))
    deployments = {
        document["metadata"]["name"]: document
        for document in documents
        if document["kind"] == "Deployment"
    }
    ports = {forward.name: forward.remote_port for forward in config.forwards}
    endpoints = {forward.name for forward in config.forwards}
    readiness_paths = {
        "config": "/api/v1/configservice/welcome",
        "station": "/api/v1/stationservice/welcome",
        "train": "/api/v1/trainservice/trains/welcome",
        "travel": "/api/v1/travelservice/welcome",
        "route": "/api/v1/routeservice/welcome",
        "price": "/api/v1/priceservice/prices/welcome",
    }
    for endpoint in endpoints:
        name = f"ts-{endpoint}-service"
        container = deployments[name]["spec"]["template"]["spec"]["containers"][0]
        probe = container["readinessProbe"]
        assert probe["httpGet"] == {
            "path": readiness_paths[endpoint],
            "port": ports[endpoint],
        }
    for endpoint in endpoints:
        probe = deployments[f"ts-{endpoint}-mongo"]["spec"]["template"]["spec"]["containers"][0][
            "readinessProbe"
        ]
        assert probe["tcpSocket"]["port"] == 27017


def test_candidate_image_is_built_once_and_reused_across_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: frontend\n",
        encoding="utf-8",
    )
    (tmp_path / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    config_data = _config(Path("manifest.yaml")).model_dump()
    config_data.update(
        {
            "kind_cluster": "test-cluster",
            "image_builds": [
                {
                    "name": "candidate",
                    "image": "candidate-${NAMESPACE}:latest",
                    "context": ".",
                }
            ],
            "image_overrides": [
                {
                    "resource": "deployment/frontend",
                    "container": "frontend",
                    "image": "${IMAGE:candidate}",
                }
            ],
        }
    )
    runner = _Runner()
    monkeypatch.setattr(KubernetesLifecycle, "_wait_http", lambda _self: None)
    lifecycle = KubernetesLifecycle(
        KubernetesConfig.model_validate(config_data),
        tmp_path,
        config_dir=tmp_path,
        runner=runner,
        popen=lambda *_args, **_kwargs: _Process(),
    )

    lifecycle.start()
    first_image = next(
        part
        for call in runner.calls
        if "set" in call and "image" in call
        for part in call
        if part.startswith("frontend=candidate-")
    )
    lifecycle.reset()
    lifecycle.close()

    assert sum(call[:2] == ["docker", "build"] for call in runner.calls) == 1
    assert sum(call[:4] == ["kind", "load", "docker-image", "--name"] for call in runner.calls) == 1
    set_images = [
        part
        for call in runner.calls
        if "set" in call and "image" in call
        for part in call
        if part.startswith("frontend=candidate-")
    ]
    assert set_images == [first_image, first_image]
    assert runner.timeouts
    assert set(runner.timeouts) == {180}
