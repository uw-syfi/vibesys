"""Best-effort GHCR storage for Docker's CI data-root archive."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from scripts.ci_image_cache import image_cache_reference

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class RegistryCommands(Protocol):
    """Bounded Docker commands; a failed command raises an exception."""

    def __call__(self, argv: Sequence[str], *, input_text: str | None = None) -> str:
        """Return stdout on success."""
        ...


def _restore(run: RegistryCommands, refs: Sequence[str], archive: Path, context: Path) -> bool:
    for index, ref in enumerate(refs):
        try:
            run(["docker", "pull", ref])
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"::warning::Image cache restore unavailable for {ref}: {exc}")
            continue
        container = run(["docker", "create", ref, "/unused"]).strip()
        try:
            staged = context / "docker-data.tar.zst"
            run(["docker", "cp", f"{container}:/docker-data.tar.zst", str(staged)])
            staged.replace(archive)
            print(f"Restored image cache {ref}")
            return index == 0
        finally:
            try:
                run(["docker", "rm", "-f", container])
            except (OSError, subprocess.SubprocessError) as exc:
                print(f"::warning::Image cache cleanup failed: {exc}")
    return False


def _publish(
    run: RegistryCommands, refs: Sequence[str], archive: Path, context: Path, source: str
) -> None:
    primary, *aliases = refs
    (context / "docker-data.tar.zst").hardlink_to(archive)
    (context / "Dockerfile").write_text(
        "FROM scratch\nARG SOURCE\nLABEL org.opencontainers.image.source=$SOURCE\n"
        "COPY docker-data.tar.zst /docker-data.tar.zst\n"
    )
    run(
        [
            "docker",
            "build",
            "--provenance=false",
            "--build-arg",
            f"SOURCE={source}",
            "--tag",
            primary,
            str(context),
        ]
    )
    run(["docker", "push", primary])
    for ref in aliases:
        run(["docker", "tag", primary, ref])
        run(["docker", "push", ref])


def registry_cache(env: Mapping[str, str], run: RegistryCommands) -> bool:
    """Restore or publish a cache; any failure warns and permits a cold build.

    The archive is staged before replacement, so a partial restore is never
    attached to Docker. Actual image builds are separate required workflow steps.
    Returns whether the exact input key was restored.
    """
    mode = env["CACHE_MODE"]
    if mode not in {"restore", "save"}:
        message = f"unknown cache mode: {mode}"
        raise ValueError(message)
    archive = Path(env["CACHE_PATH"])
    hit = False
    try:
        if mode == "restore":
            archive.unlink(missing_ok=True)
        repository = env["GITHUB_REPOSITORY"]
        keys = [env["CACHE_KEY"], *env.get("CACHE_RESTORE_KEYS", "").splitlines()]
        refs = [image_cache_reference(repository, key) for key in keys if key]
        run(
            ["docker", "login", "ghcr.io", "--username", env["GITHUB_ACTOR"], "--password-stdin"],
            input_text=env["CACHE_TOKEN"],
        )
        with tempfile.TemporaryDirectory(dir=env["RUNNER_TEMP"], prefix="image-cache-") as tmp:
            context = Path(tmp)
            if mode == "restore":
                hit = _restore(run, refs, archive, context)
            elif mode == "save":
                _publish(run, refs, archive, context, f"https://github.com/{repository}")
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"::warning::Image cache {mode} unavailable; continuing without it: {exc}")
        if mode == "restore":
            archive.unlink(missing_ok=True)
    if mode == "restore":
        with Path(env["GITHUB_OUTPUT"]).open("a") as output:
            output.write(f"cache-hit={str(hit).lower()}\n")
    return hit


def docker_command(
    argv: Sequence[str],
    *,
    input_text: str | None = None,
    timeout: float = 180,
    env: Mapping[str, str] | None = None,
) -> str:
    """Run a registry operation with one bounded deadline and captured diagnostics."""
    # lint-waiver: LW-301001 [S603]; this runner receives fixed Docker subcommands and validated refs without a shell. Duplicating subprocess calls per operation would duplicate deadlines and diagnostics; a shell wrapper would obscure the tested failure boundary.
    result = subprocess.run(  # noqa: S603
        argv,
        input=input_text,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        env=env,
    )
    if result.returncode:
        detail = (result.stderr or result.stdout)[-2000:]
        message = f"{argv[1]} failed (exit {result.returncode}): {detail}"
        raise OSError(message)
    return result.stdout


if __name__ == "__main__":
    registry_cache(os.environ, docker_command)
