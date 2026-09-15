"""Generate the minimal Train Ticket Kubernetes topology."""

# ruff: noqa: INP001

from __future__ import annotations

from pathlib import Path

import yaml

SERVICES = {
    "config": 15679,
    "station": 12345,
    "train": 14567,
    "travel": 12346,
    "route": 11178,
    "price": 16579,
}

READINESS_PATHS = {
    "config": "/api/v1/configservice/welcome",
    "station": "/api/v1/stationservice/welcome",
    "train": "/api/v1/trainservice/trains/welcome",
    "travel": "/api/v1/travelservice/welcome",
    "route": "/api/v1/routeservice/welcome",
    "price": "/api/v1/priceservice/prices/welcome",
}


def deployment(
    name: str,
    image: str,
    *,
    port: int | None = None,
    environment: dict[str, str] | None = None,
    readiness_path: str | None = None,
) -> dict[str, object]:
    """Create a single-replica Deployment."""
    container: dict[str, object] = {"name": name, "image": image, "imagePullPolicy": "IfNotPresent"}
    if port is not None:
        container["ports"] = [{"containerPort": port}]
        probe: dict[str, object] = (
            {"httpGet": {"path": readiness_path, "port": port}}
            if readiness_path is not None
            else {"tcpSocket": {"port": port}}
        )
        container["readinessProbe"] = {
            **probe,
            "periodSeconds": 2,
            "timeoutSeconds": 1,
            "failureThreshold": 450,
        }
    if environment:
        container["env"] = [{"name": key, "value": value} for key, value in environment.items()]
    pod_spec: dict[str, object] = {"containers": [container]}
    labels = {"app": name}
    deployment_spec: dict[str, object] = {
        "replicas": 1,
        "selector": {"matchLabels": labels},
        "template": {"metadata": {"labels": labels}, "spec": pod_spec},
    }
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name},
        "spec": deployment_spec,
    }


def service(
    name: str,
    port: int,
    *,
    target_port: int | None = None,
    additional_ports: dict[str, int] | None = None,
) -> dict[str, object]:
    """Create a ClusterIP Service."""
    ports = [{"port": port, "targetPort": target_port or port}]
    if additional_ports:
        ports[0]["name"] = "http"
        ports.extend(
            {"name": port_name, "port": extra_port, "targetPort": extra_port}
            for port_name, extra_port in additional_ports.items()
        )
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name},
        "spec": {
            "selector": {"app": name},
            "ports": ports,
        },
    }


def objects() -> list[dict[str, object]]:
    """Return the complete minimal topology."""
    result = []
    for short in SERVICES:
        mongo = f"ts-{short}-mongo"
        result += [deployment(mongo, "mongo:3.4", port=27017), service(mongo, 27017)]
    for short, port in SERVICES.items():
        name = f"ts-{short}-service"
        result += [
            deployment(
                name,
                f"vibesys-{short}-${{NAMESPACE}}:latest",
                port=port,
                readiness_path=READINESS_PATHS[short],
            ),
            service(name, port),
        ]
    return result


if __name__ == "__main__":
    destination = Path(__file__).with_name("manifest.yaml")
    destination.write_text(yaml.safe_dump_all(objects(), sort_keys=False), encoding="utf-8")
