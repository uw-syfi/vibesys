"""Rendering a task's manifest: substitution, the kinds allowlist, and the data-tier split. Pure."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

# Namespaced kinds an application may bring, by (API group, kind). Anything else, in
# particular every cluster-scoped kind (Namespace, ClusterRole, PersistentVolume, CRDs),
# RBAC (Role, RoleBinding) and node-wide workloads (DaemonSet), is refused: the cluster
# is shared, and a namespace deletion must remove everything a sample made.
ALLOWED_KINDS: frozenset[tuple[str, str]] = frozenset(
    {
        ("", "ConfigMap"),
        ("", "Secret"),
        ("", "Service"),
        ("", "ServiceAccount"),
        ("", "PersistentVolumeClaim"),
        ("apps", "Deployment"),
        ("apps", "StatefulSet"),
        ("policy", "PodDisruptionBudget"),
    }
)
WORKLOAD_KINDS = frozenset({"Deployment", "StatefulSet"})
_VARIABLE = re.compile(r"\$\{([^}]*)\}")


class ManifestError(ValueError):
    """The manifest cannot be applied as written; the message names the file, object or line."""


@dataclass(frozen=True)
class Rendered:
    """The manifest's objects for one namespace.

    `first` is applied and waited for before the seed, `rest` after it (empty without a
    data tier).
    """

    first: tuple[dict[str, Any], ...]
    rest: tuple[dict[str, Any], ...]
    text: str

    @property
    def objects(self) -> tuple[dict[str, Any], ...]:
        return self.first + self.rest


def substitute(text: str, values: Mapping[str, str], *, source: str) -> str:
    """Replace each `${NAME}` by values[NAME]; any other `${...}` is an error naming its line."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            line = text.count("\n", 0, match.start()) + 1
            known = ", ".join(sorted(values))
            raise ManifestError(f"{source}:{line}: unknown variable ${{{name}}} (known: {known})")
        return values[name]

    return _VARIABLE.sub(replace, text)


def _identity(obj: object, index: int, source: str) -> tuple[str, str, str]:
    if not isinstance(obj, dict):
        raise ManifestError(f"{source}: document {index + 1} is not a mapping")
    api_version, kind = obj.get("apiVersion"), obj.get("kind")
    metadata = obj.get("metadata")
    name = metadata.get("name") if isinstance(metadata, dict) else None
    if not isinstance(api_version, str) or not isinstance(kind, str) or not isinstance(name, str):
        raise ManifestError(
            f"{source}: document {index + 1} lacks apiVersion, kind or metadata.name"
        )
    group = api_version.rpartition("/")[0]
    return group, kind, name


def render(
    text: str,
    *,
    namespace: str,
    images: Mapping[str, str],
    data_tier: Mapping[str, str],
    source: str = "manifest",
) -> Rendered:
    """Substitute `${NAMESPACE}` and the images, parse, check, set the namespace, split.

    Raises ManifestError for an unknown variable, an image variable the manifest does not
    use, a kind outside ALLOWED_KINDS, an object of another namespace, a duplicate object,
    or a data tier that selects no workload.
    """
    for variable in images:
        if f"${{{variable}}}" not in text:
            raise ManifestError(f"{source}: images[{variable!r}] is not used (no ${{{variable}}})")
    rendered = substitute(text, {"NAMESPACE": namespace, **images}, source=source)
    try:
        documents = [doc for doc in yaml.safe_load_all(rendered) if doc is not None]
    except yaml.YAMLError as error:
        raise ManifestError(f"{source}: not YAML: {error}") from None
    seen: set[tuple[str, str]] = set()
    first: list[dict[str, Any]] = []
    rest: list[dict[str, Any]] = []
    for index, obj in enumerate(documents):
        group, kind, name = _identity(obj, index, source)
        where = f"{source}: {kind} {name}"
        if (group, kind) not in ALLOWED_KINDS:
            allowed = ", ".join(sorted(k for _, k in ALLOWED_KINDS))
            raise ManifestError(f"{where}: kind not allowed (allowed: {allowed})")
        metadata = obj["metadata"]
        if metadata.get("namespace", namespace) != namespace:
            raise ManifestError(f"{where}: metadata.namespace must be ${{NAMESPACE}} or absent")
        metadata["namespace"] = namespace
        if (kind, name) in seen:
            raise ManifestError(f"{where}: defined twice")
        seen.add((kind, name))
        labels = metadata.get("labels") or {}
        in_tier = kind not in WORKLOAD_KINDS or (
            bool(data_tier) and all(labels.get(k) == v for k, v in data_tier.items())
        )
        (first if in_tier or not data_tier else rest).append(obj)
    if data_tier and not any(obj["kind"] in WORKLOAD_KINDS for obj in first):
        selector = ",".join(f"{k}={v}" for k, v in data_tier.items())
        raise ManifestError(
            f"{source}: data_tier {selector!r} selects no Deployment or StatefulSet "
            "(it matches metadata.labels)"
        )
    return Rendered(first=tuple(first), rest=tuple(rest), text=rendered)


def workload_names(objects: Sequence[Mapping[str, Any]], kind: str = "Deployment") -> set[str]:
    return {obj["metadata"]["name"] for obj in objects if obj["kind"] == kind}


def service_names(objects: Sequence[Mapping[str, Any]]) -> set[str]:
    return {obj["metadata"]["name"] for obj in objects if obj["kind"] == "Service"}
