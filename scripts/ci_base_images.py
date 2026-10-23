"""Resolve CI base images from one digest-pinned inventory with a GHCR mirror.

Every image has two references to the same manifest digest: the private GHCR
mirror and the upstream registry. A job probes the mirrors once and exports the
reachable ones in ``VIBESYS_CI_BASE_IMAGES``; any other process (a developer
build, a fork PR, a mirror outage) gets the upstream reference. The digest pins
the content, so the choice never changes what is built.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import platform
import re
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from scripts.ci_registry_image_cache import docker_command

if TYPE_CHECKING:
    from collections.abc import Mapping

PINS = Path(__file__).with_suffix(".toml")
MIRROR_ROOT = "ghcr.io/uw-syfi/vibesys/ci-base"
RESOLUTION_ENV = "VIBESYS_CI_BASE_IMAGES"
_PROBE_SECONDS = 30


@dataclass(frozen=True)
class ImagePin:
    """An upstream multi-platform manifest and its byte-identical mirror."""

    source: str
    digest: str

    @property
    def upstream(self) -> str:
        """Return the immutable upstream reference: the mirror owner's source and the fallback."""
        repository = self.source.split(":", 1)[0]
        if "/" not in repository:
            repository = "library/" + repository
        registry = repository.split("/", 1)[0]
        if "." not in registry:
            repository = "docker.io/" + repository
        return f"{repository}@{self.digest}"

    @property
    def mirror(self) -> str:
        """Return the same manifest in GHCR, retaining its upstream digest."""
        return f"{MIRROR_ROOT}@{self.digest}"

    @property
    def publish_tag(self) -> str:
        """Give the manifest a retained tag without making consumers depend on it."""
        return self.mirror.split("@", 1)[0] + ":" + self.digest.replace(":", "-")


def image_pins(path: Path = PINS) -> tuple[ImagePin, ...]:
    """Validate and load the sole inventory, rejecting unknown fields and duplicates."""
    data = tomllib.loads(path.read_text())
    if set(data) != {"images"}:
        message = f"{path}: expected only images, got {sorted(data)}"
        raise ValueError(message)
    if not isinstance(data["images"], list) or not data["images"]:
        message = f"{path}: images must be a nonempty list"
        raise ValueError(message)
    pins = []
    names = set()
    for row in data["images"]:
        if not isinstance(row, dict) or set(row) != {"source", "digest"}:
            message = f"{path}: unknown or missing image fields: {row!r}"
            raise ValueError(message)
        source, digest = row["source"], row["digest"]
        if not isinstance(source, str) or not re.fullmatch(
            r"[a-z0-9][a-z0-9./_-]*:[a-zA-Z0-9_.-]+", source
        ):
            message = f"{path}: invalid source: {source!r}"
            raise ValueError(message)
        if (
            not isinstance(digest, str)
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
            or source in names
        ):
            message = f"{path}: invalid digest or duplicate source: {source}"
            raise ValueError(message)
        names.add(source)
        pins.append(ImagePin(source, digest))
    return tuple(pins)


class MirrorProbe(Protocol):
    """Check that a mirror reference can be pulled the way the job's consumers pull it."""

    def __call__(self, reference: str, /) -> None:
        """Return when the image is readable; raise ``OSError`` or ``SubprocessError``."""
        ...


class DockerRun(Protocol):
    """Run one bounded Docker command in a given environment; failure raises ``OSError``."""

    def __call__(self, argv: list[str], *, timeout: float, env: Mapping[str, str]) -> str:
        """Return stdout on success."""
        ...


CONFIG_ENV = "DOCKER_CONFIG"
_PLATFORM_ARCHITECTURES = {
    "x86_64": "amd64",
    "amd64": "amd64",
    "aarch64": "arm64",
    "arm64": "arm64",
}


def consumer_environment(env: Mapping[str, str], private_home: str) -> dict[str, str]:
    """Return the environment of a consumer whose ``HOME`` is private, as the test suite's is.

    Registry credentials reach such a consumer only through ``DOCKER_CONFIG``; without
    it the consumer pulls anonymously and a private mirror answers 401.
    """
    if not env.get(CONFIG_ENV):
        message = (
            f"{CONFIG_ENV} is not set, so a consumer with a private HOME has no registry "
            "credentials"
        )
        raise OSError(message)
    return {**env, "HOME": private_home}


def probe_mirror(reference: str, run: DockerRun, env: Mapping[str, str], machine: str) -> None:
    """Read the index and this host's platform child, with the consumers' credentials.

    A pin is a multi-platform index. Reading only the index passes while a child was
    never copied, and the pull of that child fails; so the child is read too.
    """
    raw = run(["docker", "manifest", "inspect", reference], timeout=_PROBE_SECONDS, env=env)
    try:
        children = json.loads(raw).get("manifests")
    except (ValueError, AttributeError) as exc:
        message = f"{reference} has an unreadable manifest: {exc}"
        raise OSError(message) from exc
    if children is None:
        return
    architecture = _PLATFORM_ARCHITECTURES.get(machine, machine)
    repository = reference.split("@", 1)[0]
    matching = [
        child["digest"]
        for child in children
        if child.get("platform", {}).get("os") == "linux"
        and child["platform"].get("architecture") == architecture
    ]
    if not matching:
        message = f"{reference} has no linux/{architecture} child"
        raise OSError(message)
    for digest in matching:
        run(
            ["docker", "manifest", "inspect", f"{repository}@{digest}"],
            timeout=_PROBE_SECONDS,
            env=env,
        )


def docker_mirror_probe(reference: str) -> None:
    """Probe through the Docker client with a private HOME and only ``DOCKER_CONFIG`` credentials."""
    with tempfile.TemporaryDirectory(prefix="ci-base-probe-") as home:
        env = consumer_environment(os.environ, home)
        probe_mirror(reference, docker_command, env, platform.machine())


def resolve_bases(pins: tuple[ImagePin, ...], probe: MirrorProbe) -> dict[str, str]:
    """Map each source to its mirror when readable, else to the same digest upstream.

    A mirror failure (not logged in, 401, 404, an outage, a missing Docker client)
    warns and falls back, so no registry state can fail a build the upstream could serve.
    """
    resolved = {}
    for pin in pins:
        try:
            probe(pin.mirror)
        except (OSError, subprocess.SubprocessError) as exc:
            print(
                f"::warning::CI base mirror {pin.mirror} is unavailable ({str(exc)[-300:]}); "
                f"using {pin.upstream}, the same digest",
                file=sys.stderr,
            )
            resolved[pin.source] = pin.upstream
        else:
            resolved[pin.source] = pin.mirror
    return resolved


def base_image(source: str, env: Mapping[str, str] | None = None) -> str:
    """Return the reference to build from for an upstream name; an unlisted image is an error.

    This is pure: it reads the job's exported resolution and otherwise answers with
    the upstream digest, so importing a test module never touches a registry.
    """
    env = os.environ if env is None else env
    for pin in image_pins():
        if source == pin.source:
            return _exported(env, pin)
    message = f"unmirrored CI image {source!r}; add its digest to {PINS.name}"
    raise ValueError(message)


def _exported(env: Mapping[str, str], pin: ImagePin) -> str:
    raw = env.get(RESOLUTION_ENV)
    if not raw:
        return pin.upstream
    try:
        exported = dict(json.loads(raw))
    except (TypeError, ValueError) as exc:
        message = f"{RESOLUTION_ENV} must be a JSON map of sources to references: {exc}"
        raise ValueError(message) from exc
    allowed = {p.source: (p.mirror, p.upstream) for p in image_pins()}
    for source, reference in exported.items():
        if reference not in allowed.get(source, ()):
            message = f"{RESOLUTION_ENV} has an unpinned entry for {source!r}: {reference!r}"
            raise ValueError(message)
    return exported.get(pin.source, pin.upstream)


def main(argv: list[str] | None = None, probe: MirrorProbe = docker_mirror_probe) -> None:
    """Print a base reference, the job's mirror resolution, or the mirror workflow's copy plan."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?")
    parser.add_argument("--resolve", action="store_true", help="probe mirrors, print JSON")
    args = parser.parse_args(argv)
    if args.resolve:
        print(json.dumps(resolve_bases(image_pins(), probe), sort_keys=True))
    elif args.source:
        print(base_image(args.source))
    else:
        for pin in image_pins():
            print(pin.upstream, pin.publish_tag, pin.mirror)


def ci_image_problems(root: Path, paths: list[str], pins: tuple[ImagePin, ...]) -> list[str]:
    """Check CI's literal image inputs and Dockerfiles against the pin inventory.

    Upstream names are permitted only as keys passed to ``base_image`` or the
    Feature matrix resolver. A literal reference must be a listed mirror or a
    listed upstream digest; a Docker Hub tag, or a digest outside the inventory,
    is a problem. Example task Dockerfiles are user workloads, not images built
    by CI. Dynamic FROM values come from the resolved pins or from locally built
    task/Feature stages.
    """
    problems = []
    sources = {pin.source for pin in pins}
    approved = {pin.mirror for pin in pins} | {pin.upstream for pin in pins}
    for relative in paths:
        path = root / relative
        if path.is_file() and _ci_image_source(relative):
            text = path.read_text()
            problems.extend(_literal_image_problems(relative, text, sources, approved))
    return problems


def _ci_image_source(path: str) -> bool:
    if path.startswith("examples/"):
        return False
    return (
        "Dockerfile" in Path(path).name
        or (path.startswith(".github/") and Path(path).suffix in {".yml", ".yaml", ".sh"})
        or (path.startswith("scripts/") and path.endswith(".sh"))
        or (
            path.endswith(".py")
            and (
                path.startswith(("tests/e2e/", "tests/minimal_container/", "tests/slurm_cluster/"))
                or path.endswith("_e2e.py")
            )
        )
    )


def _literal_image_problems(
    path: str, text: str, sources: set[str], approved: set[str]
) -> list[str]:
    problems = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        problems.extend(_hub_problems(path, number, stripped, approved))
        # FROM stages, shell pulls, and workflow container/service image fields.
        match = re.search(
            r"(?:^FROM\s+(?:--platform=\S+\s+)?|docker\s+pull\s+|^\s*(?:image|container):\s+)([^\s]+)",
            line,
            re.IGNORECASE if "Dockerfile" in Path(path).name else 0,
        )
        if match and not path.endswith(".py"):
            reference = match[1].strip("\"'")
            if (
                "docker.io/" not in reference  # reported above
                and reference not in approved
                and reference != "scratch"
                and not reference.startswith(("$", "{"))
            ):
                problems.append(
                    f"{path}:{number}: unlisted CI image {reference}; resolve it from ci_base_images.toml"
                )
        if "Dockerfile" in Path(path).name and stripped.startswith("FROM "):
            continue
        if path.endswith(".py"):
            continue
        # Matrix keys are resolved through the inventory before any build.
        if stripped.startswith("base:") and stripped.split(maxsplit=1)[-1] not in sources:
            problems.append(f"{path}:{number}: base is absent from ci_base_images.toml")
    if path.endswith(".py"):
        problems.extend(_python_image_problems(path, text, sources, approved))
    return problems


def _hub_problems(path: str, number: int, line: str, approved: set[str]) -> list[str]:
    hub_references = re.findall(
        r"(?:docker[.]io|registry-1[.]docker[.]io|index[.]docker[.]io)/[^\s\"']+", line
    )
    return [
        f"{path}:{number}: Docker Hub reference {hub} in CI; "
        "use a digest listed in ci_base_images.toml"
        for hub in hub_references
        if hub not in approved
    ]


def _python_image_problems(
    path: str, text: str, sources: set[str], approved: set[str]
) -> list[str]:
    tree = ast.parse(text)
    resolved = set()
    problems = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "base_image"
        ):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    resolved.add(id(arg))
                    if arg.value not in sources:
                        problems.append(
                            f"{path}:{arg.lineno}: unlisted CI image {arg.value}; add its mirror pin"
                        )
    for node in ast.walk(tree):
        if (
            not isinstance(node, ast.Constant)
            or not isinstance(node.value, str)
            or id(node) in resolved
        ):
            continue
        value = node.value
        if value in approved:
            continue
        image = re.fullmatch(
            r"(?:docker[.]io/)?[a-z0-9][a-z0-9./_-]*(?::[a-zA-Z0-9_.-]+|@sha256:[0-9a-f]+)", value
        )
        if image:
            problems.append(
                f"{path}:{node.lineno}: direct CI image {value}; use base_image with a listed pin"
            )
    return problems


if __name__ == "__main__":
    main()
