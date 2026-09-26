"""Namespace-scoped Kubernetes deployment lifecycle."""

from __future__ import annotations

import http.client
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator
from pydantic_core import PydanticCustomError

if TYPE_CHECKING:
    from collections.abc import Callable

_OWNERSHIP_LABEL = "vibesys.dev/evaluator-owned"
_MAX_NAMESPACE_PREFIX_LENGTH = 45
_ALLOWED_KINDS = frozenset(
    {
        "ConfigMap",
        "DaemonSet",
        "Deployment",
        "Job",
        "PersistentVolumeClaim",
        "Role",
        "RoleBinding",
        "Secret",
        "Service",
        "ServiceAccount",
        "StatefulSet",
    }
)
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]*[a-z0-9])?$")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HTTPProbe(_StrictModel):
    """HTTP readiness probe settings."""

    endpoint: str
    path: str
    status: int = Field(default=200, ge=100, le=599)
    json_contains: JsonValue | None = None

    @field_validator("path")
    @classmethod
    def _absolute_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise PydanticCustomError(
                "invalid_http_probe_path", "HTTP probe path must start with '/'"
            )
        return value


class ServiceForward(_StrictModel):
    """Port-forward process settings."""

    name: str
    resource: str
    remote_port: int = Field(gt=0, le=65535)
    local_port: int = Field(gt=0, le=65535)


class ImageOverride(_StrictModel):
    """Container image override settings."""

    resource: str
    container: str
    image: str


class ImageBuild(_StrictModel):
    """Container image build settings."""

    name: str
    image: str
    context: Path
    dockerfile: Path = Path("Dockerfile")
    build_args: dict[str, str] = Field(default_factory=dict)


class RestartDeployment(_StrictModel):
    """Deployment restart settings."""

    name: str
    pod_selector: str


def _json_contains(actual: JsonValue, expected: JsonValue) -> bool:
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            key in actual and _json_contains(actual[key], value) for key, value in expected.items()
        )
    if isinstance(expected, list):
        return isinstance(actual, list) and all(
            any(_json_contains(candidate, item) for candidate in actual) for item in expected
        )
    if isinstance(expected, bool) or isinstance(actual, bool):
        return type(actual) is type(expected) and actual == expected
    return actual == expected


class KubernetesConfig(_StrictModel):
    """Namespace-scoped deployment configuration."""

    context: str
    kubeconfig: Path | None = None
    namespace_prefix: str
    manifests: tuple[Path, ...]
    rollouts: tuple[str, ...]
    restart_deployments: tuple[RestartDeployment, ...] = ()
    image_builds: tuple[ImageBuild, ...] = ()
    kind_cluster: str | None = None
    image_overrides: tuple[ImageOverride, ...] = ()
    forwards: tuple[ServiceForward, ...]
    primary_endpoint: str
    http_probes: tuple[HTTPProbe, ...]
    timeout_seconds: float = Field(default=180, gt=0)

    @field_validator("namespace_prefix")
    @classmethod
    def _namespace_prefix(cls, value: str) -> str:
        if len(value) > _MAX_NAMESPACE_PREFIX_LENGTH or not _DNS_LABEL.fullmatch(value):
            raise PydanticCustomError(
                "invalid_namespace_prefix",
                "namespace_prefix must be a DNS label of at most 45 characters",
            )
        return value

    @field_validator("manifests")
    @classmethod
    def _nonempty_manifests(cls, value: tuple[Path, ...]) -> tuple[Path, ...]:
        if not value:
            raise PydanticCustomError("empty_manifests", "manifests must not be empty")
        return value

    @field_validator("forwards")
    @classmethod
    def _unique_forwards(cls, value: tuple[ServiceForward, ...]) -> tuple[ServiceForward, ...]:
        if (
            not value
            or len({item.name for item in value}) != len(value)
            or len({item.local_port for item in value}) != len(value)
        ):
            raise PydanticCustomError(
                "invalid_forwards", "forwards must contain uniquely named endpoints"
            )
        return value

    @model_validator(mode="after")
    def _references_exist(self) -> KubernetesConfig:
        endpoint_names = {item.name for item in self.forwards}
        if self.primary_endpoint not in endpoint_names:
            raise PydanticCustomError(
                "invalid_primary_endpoint", "primary_endpoint must name a configured forward"
            )
        if any(probe.endpoint not in endpoint_names for probe in self.http_probes):
            raise PydanticCustomError(
                "invalid_http_probe_endpoint", "HTTP probes must name configured forwards"
            )
        build_names = [item.name for item in self.image_builds]
        if len(set(build_names)) != len(build_names):
            raise PydanticCustomError("duplicate_image_build", "image build names must be unique")
        return self


class KubernetesLifecycleError(RuntimeError):
    """Report lifecycle failures while preserving the RuntimeError contract."""

    @classmethod
    def command_failed(
        cls, returncode: int, command: list[str], detail: str
    ) -> KubernetesLifecycleError:
        """Create an error for an unsuccessful Kubernetes command."""
        return cls(f"command failed ({returncode}): {command!r}: {detail}")

    @classmethod
    def command_timed_out(
        cls, timeout_seconds: float, command: list[str]
    ) -> KubernetesLifecycleError:
        """Create an error for a timed-out Kubernetes command."""
        return cls(f"command timed out after {timeout_seconds:g}s: {command!r}")

    @classmethod
    def lifecycle_not_started(cls) -> KubernetesLifecycleError:
        """Create an error when a lifecycle operation has not started."""
        return cls("Kubernetes lifecycle has not started")

    @classmethod
    def lifecycle_already_started(cls) -> KubernetesLifecycleError:
        """Create an error when start is called more than once."""
        return cls("Kubernetes lifecycle is already started")

    @classmethod
    def namespace_uid_unavailable(cls) -> KubernetesLifecycleError:
        """Create an error when namespace ownership cannot be recovered."""
        return cls("created namespace response has no recoverable owned UID")

    @classmethod
    def namespace_uid_missing(cls) -> KubernetesLifecycleError:
        """Create an error when the created namespace has no UID."""
        return cls("created namespace response has no UID")

    @classmethod
    def deployments_already_stopped(cls) -> KubernetesLifecycleError:
        """Create an error when configured deployments are already stopped."""
        return cls("Kubernetes deployments are already stopped")

    @classmethod
    def ownership_changed(cls, namespace: str) -> KubernetesLifecycleError:
        """Create an error when namespace ownership no longer matches."""
        return cls(f"refusing to mutate namespace {namespace!r}: ownership changed")

    @classmethod
    def port_forward_exited(cls) -> KubernetesLifecycleError:
        """Create an error when a port forward exits before readiness."""
        return cls("kubectl port-forward exited before readiness")


class KubernetesReadinessTimeoutError(TimeoutError):
    """Report that HTTP readiness probes did not pass before the deadline."""

    @classmethod
    def for_last_error(cls, last_error: str) -> KubernetesReadinessTimeoutError:
        """Create a timeout error including the last probe failure."""
        return cls(f"Kubernetes HTTP readiness timed out: {last_error}")


class KubernetesManifestMetadataError(TypeError):
    """Report a Kubernetes manifest without a metadata mapping."""

    @classmethod
    def missing(cls) -> KubernetesManifestMetadataError:
        """Create an error for a manifest without metadata."""
        return cls("manifest object must contain metadata")


class CommandRunner(Protocol):
    """Callable protocol for running commands."""

    def __call__(
        self,
        command: list[str],
        *,
        cwd: Path,
        input_text: str | None = None,
        timeout_seconds: float,
    ) -> subprocess.CompletedProcess[str]:
        """Run a command and return its completed process."""
        ...


class ForwardProcess(Protocol):
    """Manage a Kubernetes port-forward subprocess."""

    def terminate(self) -> None:
        """Request graceful process termination."""
        ...

    def kill(self) -> None:
        """Forcefully stop the process."""
        ...

    def wait(self, timeout: float | None = None) -> int:
        """Wait for process completion."""
        ...

    def poll(self) -> int | None:
        """Return the process exit status if it has exited."""
        ...


def _default_runner(
    command: list[str], *, cwd: Path, input_text: str | None = None, timeout_seconds: float
) -> subprocess.CompletedProcess[str]:
    try:
        # lint-waiver: LW-008038 [S603]; Kubernetes commands are supplied as controlled argv vectors and execute without a shell.
        return subprocess.run(  # noqa: S603
            command,
            cwd=cwd,
            input=input_text,
            text=True,
            capture_output=True,
            check=True,
            timeout=timeout_seconds,
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or "no output").strip()
        raise KubernetesLifecycleError.command_failed(error.returncode, command, detail) from error
    except subprocess.TimeoutExpired as error:
        raise KubernetesLifecycleError.command_timed_out(timeout_seconds, command) from error


def load_config(path: Path) -> KubernetesConfig:
    """Load a strict JSON or YAML lifecycle configuration."""
    return KubernetesConfig.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def _resolve_image_references(image: str, built_images: dict[str, str]) -> str:
    for name, built_image in built_images.items():
        image = image.replace(f"${{IMAGE:{name}}}", built_image)
    if "${IMAGE:" in image:
        raise PydanticCustomError(
            "unknown_image_build_reference",
            "unknown image build reference: {image}",
            {"image": image},
        )
    return image


class KubernetesLifecycle:
    """Own one fresh namespace and its foreground port-forward process."""

    def __init__(
        self,
        config: KubernetesConfig,
        candidate_dir: Path,
        *,
        config_dir: Path | None = None,
        runner: CommandRunner = _default_runner,
        popen: Callable[..., ForwardProcess] = subprocess.Popen,
    ) -> None:
        """Create the namespace-scoped deployment runtime."""
        self.config = config
        self.candidate_dir = candidate_dir.resolve()
        self.config_dir = (config_dir or candidate_dir).resolve()
        self._runner = runner
        self._popen = popen
        self._namespace: str | None = None
        self._namespace_uid: str | None = None
        self._ownership_token: str | None = None
        self._forwards: list[ForwardProcess] = []
        self._built_images: dict[str, str] | None = None
        self._stopped_replicas: tuple[tuple[RestartDeployment, int], ...] | None = None

    @property
    def namespace(self) -> str:
        """Return the runtime namespace."""
        if self._namespace is None:
            raise KubernetesLifecycleError.lifecycle_not_started()
        return self._namespace

    @property
    def base_url(self) -> str:
        """Return the service base URL."""
        return self.endpoints[self.config.primary_endpoint]

    @property
    def endpoints(self) -> dict[str, str]:
        """Return service endpoints by name."""
        endpoints = {
            item.name: f"http://127.0.0.1:{item.local_port}" for item in self.config.forwards
        }
        if self.config.primary_endpoint not in endpoints:
            raise PydanticCustomError(
                "unknown_primary_endpoint",
                "unknown primary_endpoint {primary_endpoint}",
                {"primary_endpoint": repr(self.config.primary_endpoint)},
            )
        return endpoints

    def __enter__(self) -> KubernetesLifecycle:
        """Start deployment resources and return this runtime."""
        self.start()
        return self

    def __exit__(self, *_args: object) -> None:
        """Stop resources created by this runtime."""
        self.close()

    def _kubectl(self, *arguments: str, namespaced: bool = False) -> list[str]:
        command = ["kubectl", "--context", self.config.context]
        if self.config.kubeconfig is not None:
            kubeconfig = self.config.kubeconfig
            if not kubeconfig.is_absolute():
                kubeconfig = self.config_dir / kubeconfig
            command.extend(["--kubeconfig", str(kubeconfig.resolve())])
        if namespaced:
            command.extend(["--namespace", self.namespace])
        command.extend(arguments)
        return command

    def _run(
        self, *arguments: str, namespaced: bool = False, input_text: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        return self._runner(
            self._kubectl(*arguments, namespaced=namespaced),
            cwd=self.candidate_dir,
            input_text=input_text,
            timeout_seconds=self.config.timeout_seconds,
        )

    def start(self) -> None:
        """Create and populate a fresh owned namespace, then expose its service."""
        if self._namespace is not None:
            raise KubernetesLifecycleError.lifecycle_already_started()
        namespace = f"{self.config.namespace_prefix}-{uuid.uuid4().hex[:12]}"
        manifest = self._render_manifests(namespace)
        token = uuid.uuid4().hex
        namespace_yaml = yaml.safe_dump(
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": namespace, "labels": {_OWNERSHIP_LABEL: token}},
            }
        )
        created = self._runner(
            self._kubectl("create", "--filename", "-", "--output", "json"),
            cwd=self.candidate_dir,
            input_text=namespace_yaml,
            timeout_seconds=self.config.timeout_seconds,
        )
        try:
            payload = json.loads(created.stdout)
        except json.JSONDecodeError:
            payload = {}
        uid = payload.get("metadata", {}).get("uid")
        if not isinstance(uid, str) or not uid:
            recovered = self._runner(
                self._kubectl("get", "namespace", namespace, "--output", "json"),
                cwd=self.candidate_dir,
                timeout_seconds=self.config.timeout_seconds,
            )
            recovered_payload = json.loads(recovered.stdout)
            recovered_metadata = recovered_payload.get("metadata", {})
            uid = recovered_metadata.get("uid")
            if (
                not isinstance(uid, str)
                or not uid
                or recovered_metadata.get("labels", {}).get(_OWNERSHIP_LABEL) != token
            ):
                raise KubernetesLifecycleError.namespace_uid_unavailable()
            self._namespace, self._namespace_uid, self._ownership_token = namespace, uid, token
            self.close()
            raise KubernetesLifecycleError.namespace_uid_missing()
        self._namespace = namespace
        self._namespace_uid = uid
        self._ownership_token = token
        try:
            images = self._build_images(namespace)
            self._run("apply", "--filename", "-", namespaced=True, input_text=manifest)
            for override in self.config.image_overrides:
                image = _resolve_image_references(override.image, images)
                self._run(
                    "set",
                    "image",
                    override.resource,
                    f"{override.container}={image}",
                    namespaced=True,
                )
            self._wait_rollouts()
            self._start_forwards()
            self._wait_http()
        except BaseException:
            self.close()
            raise

    def reset(self) -> None:
        """Replace the current deployment with a fresh owned namespace."""
        self.close()
        self.start()

    def restart(self) -> None:
        """Stop selected Deployments completely and restore their replica counts."""
        self.stop()
        self.start_stopped()

    def stop(self) -> None:
        """Stop configured Deployments while retaining the owned namespace."""
        if self._stopped_replicas is not None:
            raise KubernetesLifecycleError.deployments_already_stopped()
        self._verify_owned_namespace()
        self._stop_forwards()
        replicas: list[tuple[RestartDeployment, int]] = []
        for deployment in self.config.restart_deployments:
            result = self._run(
                "get",
                f"deployment/{deployment.name}",
                "--output",
                "jsonpath={.spec.replicas}",
                namespaced=True,
            )
            replicas.append((deployment, int(result.stdout or "1")))
        self._stopped_replicas = tuple(replicas)
        for deployment, _ in replicas:
            self._run("scale", f"deployment/{deployment.name}", "--replicas=0", namespaced=True)
        for deployment, _ in replicas:
            self._run(
                "wait",
                "--for=delete",
                "pod",
                "--selector",
                deployment.pod_selector,
                f"--timeout={self.config.timeout_seconds:g}s",
                namespaced=True,
            )

    def start_stopped(self) -> None:
        """Restore Deployments stopped by :meth:`stop`, forwards, and readiness."""
        replicas = self._stopped_replicas
        if replicas is None:
            self._verify_owned_namespace()
            self._wait_rollouts()
            if not self._forwards:
                self._start_forwards()
            self._wait_http()
            return
        self._verify_owned_namespace()
        for deployment, replica_count in replicas:
            self._run(
                "scale",
                f"deployment/{deployment.name}",
                f"--replicas={replica_count}",
                namespaced=True,
            )
        self._wait_rollouts(resources=tuple(f"deployment/{item.name}" for item, _ in replicas))
        self._start_forwards()
        self._wait_http()
        self._stopped_replicas = None

    @property
    def services(self) -> tuple[str, ...]:
        """Return service metadata for the deployed workloads."""
        result = self._run("get", "service", "--output", "json", namespaced=True)
        payload = json.loads(result.stdout)
        return tuple(sorted(item["metadata"]["name"] for item in payload.get("items", [])))

    def close(self) -> None:
        """Stop the forward and delete only the namespace created by this object."""
        self._stop_forwards()
        namespace, uid, token = self._namespace, self._namespace_uid, self._ownership_token
        if namespace is None or uid is None or token is None:
            return
        self._verify_owned_namespace()
        self._runner(
            self._kubectl("delete", "namespace", namespace, "--wait=true"),
            cwd=self.candidate_dir,
            timeout_seconds=self.config.timeout_seconds,
        )
        self._namespace = self._namespace_uid = self._ownership_token = None
        self._stopped_replicas = None

    def _verify_owned_namespace(self) -> None:
        namespace, uid, token = self._namespace, self._namespace_uid, self._ownership_token
        if namespace is None or uid is None or token is None:
            raise KubernetesLifecycleError.lifecycle_not_started()
        found = self._runner(
            self._kubectl("get", "namespace", namespace, "--output", "json", namespaced=False),
            cwd=self.candidate_dir,
            timeout_seconds=self.config.timeout_seconds,
        )
        payload = json.loads(found.stdout)
        labels = payload.get("metadata", {}).get("labels", {})
        if payload.get("metadata", {}).get("uid") != uid or labels.get(_OWNERSHIP_LABEL) != token:
            raise KubernetesLifecycleError.ownership_changed(namespace)

    def _stop_forwards(self) -> None:
        try:
            for forward in reversed(self._forwards):
                with suppress(ProcessLookupError):
                    forward.terminate()
                try:
                    forward.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError, subprocess.TimeoutExpired):
                        forward.kill()
                        forward.wait(timeout=5)
        finally:
            self._forwards.clear()

    def _render_manifests(self, namespace: str) -> str:
        rendered: list[str] = []
        for relative in self.config.manifests:
            raw = str(relative)
            if raw.startswith("${CANDIDATE_DIR}/"):
                root = self.candidate_dir
                path = root / raw.removeprefix("${CANDIDATE_DIR}/")
            else:
                root = self.config_dir
                path = root / relative
            path = path.resolve()
            if relative.is_absolute() or not path.is_relative_to(root) or not path.is_file():
                raise PydanticCustomError(
                    "invalid_manifest_path",
                    "manifest escapes its trusted root or is not a file: {relative}",
                    {"relative": str(relative)},
                )
            text = path.read_text(encoding="utf-8").replace("${NAMESPACE}", namespace)
            for document in yaml.safe_load_all(text):
                if document is None:
                    continue
                if not isinstance(document, dict) or document.get("kind") not in _ALLOWED_KINDS:
                    kind = document.get("kind") if isinstance(document, dict) else None
                    raise PydanticCustomError(
                        "unsupported_manifest_kind",
                        "unsupported or cluster-scoped manifest kind: {kind}",
                        {"kind": repr(kind)},
                    )
                metadata = document.get("metadata")
                if not isinstance(metadata, dict):
                    raise KubernetesManifestMetadataError.missing()
                declared = metadata.get("namespace")
                if declared not in {None, namespace}:
                    raise PydanticCustomError(
                        "manifest_namespace_mismatch",
                        "manifest namespace {declared} does not match {namespace}",
                        {"declared": repr(declared), "namespace": repr(namespace)},
                    )
                metadata["namespace"] = namespace
                rendered.append(yaml.safe_dump(document, sort_keys=True))
        if not rendered:
            raise PydanticCustomError("empty_manifests", "manifests contain no Kubernetes objects")
        return "---\n".join(rendered)

    def _candidate_path(self, relative: Path) -> Path:
        resolved = (self.candidate_dir / relative).resolve()
        if relative.is_absolute() or not resolved.is_relative_to(self.candidate_dir):
            raise PydanticCustomError(
                "invalid_candidate_path",
                "candidate path escapes candidate directory: {relative}",
                {"relative": str(relative)},
            )
        return resolved

    def _dockerfile_path(self, configured: Path) -> Path:
        raw = str(configured)
        if raw.startswith("${CONFIG_DIR}/"):
            root = self.config_dir
            path = root / raw.removeprefix("${CONFIG_DIR}/")
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                raise PydanticCustomError(
                    "invalid_dockerfile_path",
                    "Dockerfile escapes config directory: {configured}",
                    {"configured": str(configured)},
                )
            return resolved
        return self._candidate_path(configured)

    def _build_images(self, namespace: str) -> dict[str, str]:
        if self._built_images is not None:
            return self._built_images
        images: dict[str, str] = {}
        for build in self.config.image_builds:
            image = build.image.replace("${NAMESPACE}", namespace)
            context = self._candidate_path(build.context)
            dockerfile = self._dockerfile_path(build.dockerfile)
            build_arguments = [
                part
                for name, value in build.build_args.items()
                for part in ("--build-arg", f"{name}={value}")
            ]
            self._runner(
                [
                    "docker",
                    "build",
                    "--tag",
                    image,
                    "--file",
                    str(dockerfile),
                    *build_arguments,
                    str(context),
                ],
                cwd=self.candidate_dir,
                timeout_seconds=self.config.timeout_seconds,
            )
            if self.config.kind_cluster:
                self._runner(
                    ["kind", "load", "docker-image", "--name", self.config.kind_cluster, image],
                    cwd=self.candidate_dir,
                    timeout_seconds=self.config.timeout_seconds,
                )
            images[build.name] = image
        self._built_images = images
        return images

    def _wait_rollouts(self, *, resources: tuple[str, ...] | None = None) -> None:
        for resource in resources if resources is not None else self.config.rollouts:
            self._run(
                "rollout",
                "status",
                resource,
                f"--timeout={self.config.timeout_seconds:g}s",
                namespaced=True,
            )

    def _start_forwards(self) -> None:
        for forward in self.config.forwards:
            self._forwards.append(
                self._popen(
                    self._kubectl(
                        "port-forward",
                        forward.resource,
                        f"{forward.local_port}:{forward.remote_port}",
                        namespaced=True,
                    ),
                    cwd=self.candidate_dir,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                )
            )

    def _wait_http(self) -> None:
        deadline = time.monotonic() + self.config.timeout_seconds
        last_error = "no attempt"
        while time.monotonic() < deadline:
            if any(forward.poll() is not None for forward in self._forwards):
                raise KubernetesLifecycleError.port_forward_exited()
            try:
                for probe in self.config.http_probes:
                    url = f"{self.endpoints[probe.endpoint]}{probe.path}"
                    opener = urllib.request.OpenerDirector()
                    opener.add_handler(urllib.request.HTTPHandler())
                    opener.add_handler(urllib.request.HTTPSHandler())
                    with opener.open(url, timeout=2) as response:
                        if response.status != probe.status:
                            last_error = (
                                f"{probe.path} returned {response.status}, expected {probe.status}"
                            )
                            break
                        if probe.json_contains is not None:
                            payload = json.load(response)
                            if not _json_contains(payload, probe.json_contains):
                                last_error = (
                                    f"{probe.path} JSON does not contain {probe.json_contains!r}"
                                )
                                break
                else:
                    return
            except (
                OSError,
                TimeoutError,
                ValueError,
                urllib.error.URLError,
                http.client.HTTPException,
            ) as error:
                last_error = str(error)
            time.sleep(0.25)
        raise KubernetesReadinessTimeoutError.for_last_error(last_error)
