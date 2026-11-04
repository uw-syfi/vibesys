"""Turn an ordered list of Dev Container Features into a Dockerfile, with no I/O.

The Dev Container Features specification says what a Feature build does: for
each Feature in order, make its folder available, export its options as
environment variables, run its ``install.sh`` as root, and apply its
``containerEnv``. The reference ``devcontainer`` CLI does that by driving
Docker, which needs the Docker socket inside a container. VibeSys instead
writes the same steps as a Dockerfile and runs plain ``docker build``, so a
host needs only Docker, and a host whose daemon refuses to bind-mount its socket
still works. The CLI remains the authoring and CI tool
(``devcontainer features test``), which keeps proving the Features conform to
the specification this module implements.

Only the metadata fields VibeSys's Features use are supported (see
:data:`SUPPORTED_METADATA_FIELDS`). Anything else, such as ``mounts``,
``privileged`` or ``entrypoint``, which this build path could not honour, is
rejected by name instead of being ignored.

:func:`plan_feature_build` is pure: the same Features, options, user, and
working directory give the same Dockerfile and context files, so the build
cache key depends only on what was selected.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from pydantic import JsonValue, TypeAdapter

from vs_decode.api import Unreadable, decode_json

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

#: Metadata fields this build path supports. ``installsAfter`` is checked
#: against the given order; ``dependsOn`` must be empty, because VibeSys lists
#: every Feature a build needs explicitly.
SUPPORTED_METADATA_FIELDS = frozenset(
    {
        "id", "version", "name", "description", "documentationURL", "options",
        "containerEnv", "installsAfter", "dependsOn",
    }
)  # fmt: skip

#: Where each Feature's folder is copied to, inside the image, while it installs.
INSTALL_ROOT = "/usr/src/vibesys-features"

#: The name of the generated file, in each Feature's folder, that holds its options.
OPTIONS_FILE = "devcontainer-features.env"

#: The build argument naming the base image.
BASE_IMAGE_ARG = "BASE_IMAGE"

_FEATURES_CONTEXT_DIR = "features"
_PLAIN_NAME = re.compile(r"[a-z_][a-z0-9_-]*")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_OPTION_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_OPTION_TYPES = frozenset({"string", "boolean"})
_ROOT_HOME = "/root"


class FeatureSpecError(ValueError):
    """A Feature's metadata or options cannot be built by this module."""


@dataclass(frozen=True)
class FeatureSpec:
    """The parts of a Feature's ``devcontainer-feature.json`` this build uses."""

    feature_id: str
    option_defaults: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    container_env: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))
    installs_after: tuple[str, ...] = ()


@dataclass(frozen=True)
class FeatureInstall:
    """One Feature in a build: its metadata, its source folder, and the options to pass.

    *source* is opaque to the planner; it is handed back in
    :attr:`BuildPlan.copied_folders` for the caller to copy.
    """

    spec: FeatureSpec
    source: Path
    options: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


@dataclass(frozen=True)
class BuildPlan:
    """What a ``docker build`` needs: the Dockerfile and the files of its context.

    ``copied_folders`` maps a context-relative destination to the Feature folder
    to copy there; ``generated_files`` maps a context-relative path to its text
    and is written after the copies.
    """

    dockerfile: str
    copied_folders: Mapping[str, Path]
    generated_files: Mapping[str, str]


_ANY_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)
_MAX_FEATURE_METADATA_BYTES = 1024 * 1024
"""The largest ``devcontainer-feature.json`` read."""
"""Any JSON value; pydantic's parser bounds nesting, so deep input is invalid, not a crash."""


def option_env_name(option_id: str) -> str:
    """Return the environment variable a Feature option is exported as.

    The specification derives it from the option id: every character that is
    not a word character becomes ``_``, leading digits and underscores become a
    single ``_``, and the result is upper-cased.
    """
    sanitized = re.sub(r"[^A-Za-z0-9_]", "_", option_id)
    return re.sub(r"^[0-9_]+", "_", sanitized).upper()


def parse_feature_metadata(text: str, *, folder_name: str) -> FeatureSpec:
    """Parse a ``devcontainer-feature.json`` document.

    Raises:
        FeatureSpecError: The document is not a JSON object, names a different
            id than its folder, uses a field this build path does not support,
            or declares an option without a usable type and default.
    """
    document: Any = decode_json(
        text, _ANY_JSON, source="devcontainer-feature.json", max_bytes=_MAX_FEATURE_METADATA_BYTES
    )
    if isinstance(document, Unreadable):
        reason = document.problems[0].message
        message = f"Feature {folder_name}: devcontainer-feature.json is not valid JSON: {reason}"
        raise FeatureSpecError(message)
    if not isinstance(document, dict):
        message = f"Feature {folder_name}: devcontainer-feature.json must be a JSON object"
        raise FeatureSpecError(message)
    unsupported = sorted(set(document) - SUPPORTED_METADATA_FIELDS)
    if unsupported:
        message = (
            f"Feature {folder_name}: unsupported metadata field {unsupported!r}; "
            f"supported fields are {sorted(SUPPORTED_METADATA_FIELDS)!r}"
        )
        raise FeatureSpecError(message)
    if document.get("id") != folder_name:
        message = (
            f"Feature {folder_name}: metadata id {document.get('id')!r} differs from the folder"
        )
        raise FeatureSpecError(message)
    if document.get("dependsOn"):
        message = (
            f"Feature {folder_name}: dependsOn is not supported; list every Feature in the build"
        )
        raise FeatureSpecError(message)
    return FeatureSpec(
        feature_id=folder_name,
        option_defaults=MappingProxyType(
            _option_defaults(folder_name, document.get("options", {}))
        ),
        container_env=MappingProxyType(
            _container_env(folder_name, document.get("containerEnv", {}))
        ),
        installs_after=tuple(_string_list(folder_name, "installsAfter", document)),
    )


def plan_feature_build(features: Sequence[FeatureInstall], *, user: str, workdir: str) -> BuildPlan:
    """Return the Dockerfile and context that apply *features*, in order, then start as *user*.

    The Dockerfile begins ``FROM ${BASE_IMAGE}`` (the caller passes the base as
    a build argument), installs each Feature as root, and ends with ``WORKDIR``
    and ``USER`` so the image starts as *user* in *workdir* without flags. The
    Features are copied one at a time, each just before its install step, so
    changing one Feature leaves the layers before it cached.

    Raises:
        FeatureSpecError: *user* or *workdir* is malformed, two Features share
            an id, an option is not declared by its Feature or has a value that
            cannot be exported, two options export the same variable, or a
            Feature's ``installsAfter`` names a Feature placed after it.
    """
    if _PLAIN_NAME.fullmatch(user) is None:
        message = f"user must be a plain user name: {user!r}"
        raise FeatureSpecError(message)
    if not workdir.startswith("/") or any(ch.isspace() or ch == '"' for ch in workdir):
        message = f"workdir must be an absolute path without whitespace: {workdir!r}"
        raise FeatureSpecError(message)
    ids = [install.spec.feature_id for install in features]
    duplicates = sorted({feature_id for feature_id in ids if ids.count(feature_id) > 1})
    if duplicates:
        message = f"Feature listed twice: {duplicates!r}"
        raise FeatureSpecError(message)
    _check_install_order(features)

    lines = [f"ARG {BASE_IMAGE_ARG}", f"FROM ${{{BASE_IMAGE_ARG}}}", "USER root"]
    copied: dict[str, Path] = {}
    generated: dict[str, str] = {}
    for install in features:
        spec = install.spec
        context_dir = f"{_FEATURES_CONTEXT_DIR}/{spec.feature_id}"
        image_dir = f"{INSTALL_ROOT}/{spec.feature_id}"
        copied[context_dir] = install.source
        generated[f"{context_dir}/{OPTIONS_FILE}"] = _options_file(install)
        lines += [
            f"# Feature {spec.feature_id}",
            f"COPY {context_dir} {image_dir}",
            f"RUN cd {image_dir} \\",
            " && chmod +x ./install.sh \\",
            f" && set -a && . ./{OPTIONS_FILE} && set +a \\",
            " && ./install.sh \\",
            f" && rm -rf {image_dir}",
        ]
        lines += [
            f'ENV {name}="{_escape_env(value)}"' for name, value in spec.container_env.items()
        ]
    lines += [f"WORKDIR {workdir}", f"USER {user}", ""]
    return BuildPlan(
        dockerfile="\n".join(lines),
        copied_folders=MappingProxyType(copied),
        generated_files=MappingProxyType(generated),
    )


def _options_file(install: FeatureInstall) -> str:
    """Return the shell-sourceable options file: defaults, then the given options."""
    spec = install.spec
    undeclared = sorted(set(install.options) - set(spec.option_defaults))
    if undeclared:
        message = f"Feature {spec.feature_id} declares no option {undeclared!r}"
        raise FeatureSpecError(message)
    values = {**spec.option_defaults, **install.options}
    exported: dict[str, str] = {}
    for option, value in values.items():
        name = option_env_name(option)
        if name in exported:
            message = f"Feature {spec.feature_id}: options collide on environment variable {name}"
            raise FeatureSpecError(message)
        if "\n" in value or "\x00" in value:
            message = (
                f"Feature {spec.feature_id}: option {option} has a newline or NUL in its value"
            )
            raise FeatureSpecError(message)
        exported[name] = value
    # The variables the reference CLI also provides: this build runs as root.
    context = {
        "_CONTAINER_USER": "root",
        "_REMOTE_USER": "root",
        "_CONTAINER_USER_HOME": _ROOT_HOME,
        "_REMOTE_USER_HOME": _ROOT_HOME,
    }
    return "".join(
        f"{name}={shlex.quote(value)}\n" for name, value in {**context, **exported}.items()
    )


def _check_install_order(features: Sequence[FeatureInstall]) -> None:
    position = {install.spec.feature_id: index for index, install in enumerate(features)}
    for index, install in enumerate(features):
        for earlier in install.spec.installs_after:
            if position.get(earlier, -1) > index:
                message = (
                    f"Feature {install.spec.feature_id} must install after {earlier}, "
                    "which is listed later"
                )
                raise FeatureSpecError(message)


def _escape_env(value: str) -> str:
    if "\n" in value or "\x00" in value:
        message = f"containerEnv value has a newline or NUL: {value!r}"
        raise FeatureSpecError(message)
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _option_defaults(folder_name: str, options: object) -> dict[str, str]:
    if not isinstance(options, dict):
        message = f"Feature {folder_name}: options must be an object"
        raise FeatureSpecError(message)
    defaults: dict[str, str] = {}
    for option, spec in options.items():
        if _OPTION_ID.fullmatch(option) is None:
            message = f"Feature {folder_name}: option id {option!r} is not a word-character name"
            raise FeatureSpecError(message)
        if not isinstance(spec, dict) or spec.get("type") not in _OPTION_TYPES:
            message = f"Feature {folder_name}: option {option} needs type 'string' or 'boolean'"
            raise FeatureSpecError(message)
        if "default" not in spec:
            message = f"Feature {folder_name}: option {option} has no default"
            raise FeatureSpecError(message)
        default = spec["default"]
        defaults[option] = str(default).lower() if isinstance(default, bool) else str(default)
    return defaults


def _container_env(folder_name: str, env: object) -> dict[str, str]:
    if not isinstance(env, dict):
        message = f"Feature {folder_name}: containerEnv must be an object"
        raise FeatureSpecError(message)
    for name, value in env.items():
        if _ENV_NAME.fullmatch(name) is None or not isinstance(value, str):
            message = (
                f"Feature {folder_name}: containerEnv {name!r} must map a variable name to a string"
            )
            raise FeatureSpecError(message)
    return dict(env)


def _string_list(folder_name: str, key: str, document: Mapping[str, Any]) -> list[str]:
    value = document.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        message = f"Feature {folder_name}: {key} must be a list of strings"
        raise FeatureSpecError(message)
    return value
