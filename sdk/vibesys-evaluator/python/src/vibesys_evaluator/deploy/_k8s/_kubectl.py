"""`KubectlAPI`: the `KubeAPI` over the kubectl binary and the kubeconfig in the environment."""

from __future__ import annotations

import io
import json
import subprocess
import tarfile
import tempfile
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from vibesys_evaluator.deploy._k8s._api import (
    ConflictError,
    ExecResult,
    KubeError,
    Namespace,
    NotFoundError,
    Pod,
    PodStats,
    Workload,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

FIELD_MANAGER = "vibesys-evaluator"
CALL_TIMEOUT_SECS = 60.0
APPLY_TIMEOUT_SECS = 180.0
COPY_TIMEOUT_SECS = 600.0


def _stamp(value: str) -> float:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()


def _labels(metadata: Mapping[str, Any]) -> dict[str, str]:
    return dict(metadata.get("labels") or {})


def parse_namespace(item: Mapping[str, Any]) -> Namespace:
    meta = item["metadata"]
    return Namespace(
        name=meta["name"],
        uid=meta["uid"],
        labels=_labels(meta),
        created=_stamp(meta["creationTimestamp"]),
        deleting=bool(meta.get("deletionTimestamp")),
    )


def parse_pod(item: Mapping[str, Any]) -> Pod:
    meta, spec, status = item["metadata"], item.get("spec", {}), item.get("status", {})
    conditions = {c.get("type"): c for c in status.get("conditions", [])}
    statuses = status.get("containerStatuses", [])
    waiting = next(
        (
            s["state"]["waiting"].get("reason", "")
            for s in statuses
            if "waiting" in s.get("state", {})
        ),
        "",
    )
    scheduled = conditions.get("PodScheduled", {})
    if not waiting and scheduled.get("status") == "False":
        waiting = scheduled.get("reason", "Unschedulable")
    terminated = statuses[0].get("state", {}).get("terminated") if statuses else None
    return Pod(
        name=meta["name"],
        uid=meta["uid"],
        labels=_labels(meta),
        phase=status.get("phase", "Unknown"),
        ready=conditions.get("Ready", {}).get("status") == "True",
        deleting=bool(meta.get("deletionTimestamp")),
        node=spec.get("nodeName"),
        restarts=sum(int(s.get("restartCount", 0)) for s in statuses),
        waiting=waiting,
        exit_code=int(terminated["exitCode"]) if terminated else None,
    )


def parse_workload(item: Mapping[str, Any]) -> Workload:
    meta, spec, status = item["metadata"], item.get("spec", {}), item.get("status", {})
    return Workload(
        kind=item["kind"],
        name=meta["name"],
        labels=_labels(meta),
        selector=dict(spec.get("selector", {}).get("matchLabels") or {}),
        replicas=int(spec.get("replicas", 1)),
        ready=int(status.get("readyReplicas", 0)),
        available=int(status.get("availableReplicas", 0)),
        updated=int(status.get("updatedReplicas", 0)),
        generation=int(meta.get("generation", 0)),
        observed=int(status.get("observedGeneration", 0)),
    )


def _selector(labels: Mapping[str, str]) -> str:
    return ",".join(f"{k}={v}" for k, v in sorted(labels.items()))


class KubectlAPI:
    """Each call is one kubectl process with a deadline; failures are KubeError with stderr."""

    def __init__(self, *, env: Mapping[str, str] | None = None, kubectl: str = "kubectl") -> None:
        self._env = dict(env) if env is not None else None
        self._kubectl = kubectl

    def _run(
        self,
        args: Sequence[str],
        *,
        stdin: bytes | None = None,
        timeout: float = CALL_TIMEOUT_SECS,
        check: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        argv = [self._kubectl, *args]
        try:
            # lint-waiver: LW-561001 [S603]; kubectl is run with an argv list, never a shell.
            # > This is the one place the deploy layer reaches kubectl; a client library would add a
            # > dependency and a second implementation of kubeconfig handling for the same calls.
            done = subprocess.run(  # noqa: S603
                argv, input=stdin, capture_output=True, timeout=timeout, env=self._env, check=False
            )
        except subprocess.TimeoutExpired:
            raise KubeError(f"kubectl {' '.join(args[:3])}: no answer in {timeout:.0f}s") from None
        if check and done.returncode != 0:
            message = done.stderr.decode(errors="replace").strip()[-2000:]
            if "(NotFound)" in message:
                raise NotFoundError(message)
            if "(Conflict)" in message or "Precondition failed" in message:
                raise ConflictError(message)
            raise KubeError(f"kubectl {' '.join(args[:4])} failed ({done.returncode}): {message}")
        return done

    def _json(self, args: Sequence[str], *, stdin: bytes | None = None) -> dict[str, Any]:
        found = json.loads(self._run([*args, "-o", "json"], stdin=stdin).stdout)
        if not isinstance(found, dict):
            raise KubeError(f"kubectl {' '.join(args[:3])}: not a JSON object")
        return found

    def create_namespace(self, name: str, labels: Mapping[str, str]) -> Namespace:
        body = {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": name, "labels": dict(labels)},
        }
        return parse_namespace(self._json(["create", "-f", "-"], stdin=json.dumps(body).encode()))

    def namespaces(self, selector: Mapping[str, str]) -> list[Namespace]:
        items = self._json(["get", "namespaces", "-l", _selector(selector)])["items"]
        return [parse_namespace(item) for item in items]

    def delete_namespace(self, name: str, *, uid: str) -> None:
        body = {
            "kind": "DeleteOptions",
            "apiVersion": "v1",
            "preconditions": {"uid": uid},
            "propagationPolicy": "Background",
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json") as options:
            json.dump(body, options)
            options.flush()
            self._run(["delete", "--raw", f"/api/v1/namespaces/{name}", "-f", options.name])

    def apply(self, namespace: str, objects: Sequence[Mapping[str, Any]]) -> None:
        body = {"apiVersion": "v1", "kind": "List", "items": list(objects)}
        self._run(
            [
                "apply",
                "-n",
                namespace,
                "--server-side",
                "--force-conflicts",
                f"--field-manager={FIELD_MANAGER}",
                "-f",
                "-",
            ],
            stdin=json.dumps(body).encode(),
            timeout=APPLY_TIMEOUT_SECS,
        )

    def workloads(self, namespace: str) -> list[Workload]:
        items = self._json(["get", "deployments.apps,statefulsets.apps", "-n", namespace])["items"]
        return [parse_workload(item) for item in items]

    def pods(self, namespace: str, selector: Mapping[str, str] | None = None) -> list[Pod]:
        args = ["get", "pods", "-n", namespace] + (["-l", _selector(selector)] if selector else [])
        return [parse_pod(item) for item in self._json(args)["items"]]

    def delete_pods(self, namespace: str, names: Sequence[str], *, grace_secs: int) -> None:
        if names:
            self._run(
                [
                    "delete",
                    "pod",
                    "-n",
                    namespace,
                    *names,
                    f"--grace-period={grace_secs}",
                    "--wait=false",
                    "--ignore-not-found",
                ]
            )

    def scale(self, namespace: str, deployment: str, replicas: int) -> None:
        self._run(["scale", "-n", namespace, f"deployment/{deployment}", f"--replicas={replicas}"])

    def exec(self, namespace: str, pod: str, argv: Sequence[str], *, timeout: float) -> ExecResult:
        done = self._run(["exec", "-n", namespace, pod, "--", *argv], timeout=timeout, check=False)
        output = (done.stdout + done.stderr).decode(errors="replace")
        if done.returncode != 0 and (
            "Error from server" in output or "error: unable to upgrade" in output
        ):
            raise KubeError(f"kubectl exec {pod}: {output.strip()[-1500:]}")
        return ExecResult(exit_code=done.returncode, output=output)

    def put_files(
        self, namespace: str, pod: str, directory: str, files: Mapping[str, bytes]
    ) -> None:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for name, data in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o644
                archive.addfile(info, io.BytesIO(data))
        self._run(
            [
                "exec",
                "-i",
                "-n",
                namespace,
                pod,
                "--",
                "tar",
                "-x",
                "-m",
                "-C",
                directory,
                "-f",
                "-",
            ],
            stdin=buffer.getvalue(),
            timeout=COPY_TIMEOUT_SECS,
        )

    def get_files(self, namespace: str, pod: str, directory: str, into: Path) -> list[str]:
        into.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile() as stream:
            argv = [
                self._kubectl,
                "exec",
                "-n",
                namespace,
                pod,
                "--",
                "tar",
                "-c",
                "-C",
                directory,
                "-f",
                "-",
                ".",
            ]
            try:
                # lint-waiver: LW-561002 [S603]; kubectl exec with an argv list, never a shell.
                # > Streaming tar to a file needs stdout to a file object, which _run's capture
                # > would hold in memory; a second helper for one call adds indirection only.
                done = subprocess.run(  # noqa: S603
                    argv,
                    stdout=stream,
                    stderr=subprocess.PIPE,
                    timeout=COPY_TIMEOUT_SECS,
                    env=self._env,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                raise KubeError(
                    f"copying {directory} out of {pod}: no end in {COPY_TIMEOUT_SECS:.0f}s"
                ) from None
            if done.returncode != 0:
                raise KubeError(
                    f"copying {directory} out of {pod} failed: "
                    f"{done.stderr.decode(errors='replace')[-1500:]}"
                )
            stream.seek(0)
            with tarfile.open(fileobj=stream, mode="r|") as archive:
                names: list[str] = []
                for member in archive:
                    if member.isfile():
                        names.append(member.name.removeprefix("./"))
                    archive.extract(member, into, filter="data")
        return sorted(names)

    def logs(
        self, namespace: str, pod: str, *, tail: int | None = None, previous: bool = False
    ) -> str:
        args = (
            ["logs", "-n", namespace, pod]
            + ([f"--tail={tail}"] if tail else [])
            + (["--previous"] if previous else [])
        )
        done = self._run(args, check=False, timeout=COPY_TIMEOUT_SECS)
        return done.stdout.decode(errors="replace") if done.returncode == 0 else ""

    def pod_stats(self, namespace: str, node: str) -> dict[str, PodStats]:
        summary = json.loads(
            self._run(["get", "--raw", f"/api/v1/nodes/{node}/proxy/stats/summary"]).stdout
        )
        stats: dict[str, PodStats] = {}
        for pod in summary.get("pods", []):
            ref = pod.get("podRef", {})
            if ref.get("namespace") == namespace:
                network = pod.get("network") or {}
                stats[ref["name"]] = PodStats(
                    cpu_seconds=(pod.get("cpu") or {}).get("usageCoreNanoSeconds", 0) / 1e9,
                    rx_bytes=int(network.get("rxBytes", 0)),
                    tx_bytes=int(network.get("txBytes", 0)),
                )
        return stats
