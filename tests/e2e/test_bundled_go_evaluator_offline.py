"""Resolved Go evaluator packages prepare dependencies before trusted gates run."""

from __future__ import annotations

import os
import shlex
import shutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from tests.support import run_test_command

from vibesys.config import BUNDLED_RESOURCES
from vs_runtime.api.infrastructure import (
    EvaluatorGoModule,
    TrustedEvaluationCommandPaths,
    TrustedEvaluationPlan,
    TrustedEvaluatorRequirements,
    evaluator_go_module_ready_path,
    evaluator_go_module_setup_command,
    load_evaluator_package,
    prepare_trusted_evaluation_plan,
    prepare_trusted_evaluator,
)

if TYPE_CHECKING:
    import subprocess


def _go_entrypoints() -> dict[str, tuple[str, ...]]:
    packages_root = BUNDLED_RESOURCES.directory("evaluators")
    assert packages_root is not None
    return {
        package_root.name: tuple(
            name
            for name, command in load_evaluator_package(package_root).metadata.entrypoints.items()
            if "go" in command and "run" in command
        )
        for package_root in sorted(packages_root.iterdir())
        if package_root.is_dir()
        and (package_root / "vibesys.evaluator.toml").is_file()
        and any(
            "go" in command and "run" in command
            for command in load_evaluator_package(package_root).metadata.entrypoints.values()
        )
    }


def _make_cache_tree_deletable(root: Path) -> None:
    """Restore directory write bits so pytest can remove Go's read-only cache trees."""
    for path in (root, *root.rglob("*")):
        if path.is_dir():
            path.chmod(0o755)


def _successful_entrypoint_arguments(
    package_name: str,
    workspace: Path,
) -> tuple[str, ...]:
    """Return a bounded successful invocation for each manifest-declared Go gate."""
    if package_name == "microservice":
        return ("-h",)
    return (
        "check",
        "--use-reference",
        "--workspace",
        str(workspace),
        "--operations",
        "1",
        "--trials",
        "1",
    )


def _assert_successful_offline(result: subprocess.CompletedProcess[str]) -> None:
    diagnostic = result.stdout + result.stderr
    assert result.returncode == 0, diagnostic
    assert "module lookup disabled" not in diagnostic
    assert "go: downloading" not in diagnostic


def _assert_incomplete_cache_is_rejected(
    package_root: Path,
    cache_root: Path,
    requirements: TrustedEvaluatorRequirements,
    environment: dict[str, str],
    tmp_path: Path,
) -> None:
    for damage in ("missing", "modified"):
        incomplete_cache = tmp_path / f"{damage}-stack-cache"
        shutil.copytree(cache_root, incomplete_cache)
        cached_source = next(
            path
            for path in incomplete_cache.rglob("*.go")
            if "cache/download" not in path.relative_to(incomplete_cache).as_posix()
        )
        cached_source.chmod(0o644)
        if damage == "missing":
            cached_source.parent.chmod(0o755)
            cached_source.unlink()
        else:
            cached_source.write_text("package corrupt\n", encoding="utf-8")
        setup = evaluator_go_module_setup_command(
            requirements,
            runtime_package_root=str(package_root),
            runtime_cache_root=str(incomplete_cache),
        )

        result = run_test_command(
            ["sh", "-c", setup],
            env={**environment, "GOPROXY": "off", "GOSUMDB": "off"},
            capture_output=True,
            text=True,
            timeout=180,
        )

        assert result.returncode != 0
        assert "evaluator Go module cache is incomplete after preparation" in (
            result.stdout + result.stderr
        )


@pytest.mark.native_build
def test_concurrent_first_use_allocates_filesystem_unique_verification_caches(
    tmp_path: Path,
) -> None:
    """Separate PID namespaces cannot collide while publishing one shared cache."""
    package_root = tmp_path / "package"
    package_root.mkdir()
    cache_root = tmp_path / "go-modules"
    runtime_scratch = tmp_path / "runtime-scratch"
    runtime_scratch.mkdir()
    module = EvaluatorGoModule(
        root=package_root,
        cache_root=cache_root,
        package_digest="sha256:" + "a" * 64,
    )
    requirements = TrustedEvaluatorRequirements(
        package_root=package_root,
        toolchains=frozenset({"go"}),
        go_module=module,
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    go_log = tmp_path / "go-list-caches"
    fake_go = fake_bin / "go"
    fake_go.write_text(
        """#!/bin/sh
case " $* " in
  *" list -deps ./... "*) printf '%s\\n' "$GOCACHE" >> "$FAKE_GO_LOG" ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    fake_go.chmod(0o755)
    setup = evaluator_go_module_setup_command(
        requirements,
        runtime_package_root=str(package_root),
        runtime_cache_root=str(cache_root),
    )
    concurrent_setup = f"""set -e
(
{setup}
) &
first=$!
(
{setup}
) &
second=$!
wait "$first"
wait "$second"
"""

    result = run_test_command(
        ["sh", "-c", concurrent_setup],
        env={
            **os.environ,
            "FAKE_GO_LOG": str(go_log),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "TMPDIR": str(runtime_scratch),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    verification_caches = go_log.read_text(encoding="utf-8").splitlines()
    assert len(verification_caches) == 2
    assert len(set(verification_caches)) == 2
    assert evaluator_go_module_ready_path(module, cache_root).is_dir()
    verification_scratch = runtime_scratch / f"vibesys-evaluator-go-verify-{os.getuid()}"
    assert not tuple(verification_scratch.glob("cache.*"))
    assert not tuple(cache_root.glob(".verification-build.*"))
    assert not tuple(cache_root.glob(".vibesys-ready.tmp.*"))


@pytest.mark.native_build
def test_interrupted_verification_cleans_scratch_and_recovers_abandoned_work(
    tmp_path: Path,
) -> None:
    """An interrupted verifier cleans itself and recovers unlocked crash residue."""
    package_root = tmp_path / "package"
    package_root.mkdir()
    cache_root = tmp_path / "go-modules"
    cache_root.mkdir()
    runtime_scratch = tmp_path / "runtime-scratch"
    runtime_scratch.mkdir()
    module = EvaluatorGoModule(
        root=package_root,
        cache_root=cache_root,
        package_digest="sha256:" + "b" * 64,
    )
    requirements = TrustedEvaluatorRequirements(
        package_root=package_root,
        toolchains=frozenset({"go"}),
        go_module=module,
    )
    evaluator_go_module_ready_path(module, cache_root).mkdir()
    verification_scratch = runtime_scratch / f"vibesys-evaluator-go-verify-{os.getuid()}"
    abandoned = verification_scratch / "cache.abandoned"
    abandoned.mkdir(parents=True)
    (verification_scratch / ".lease.cache.abandoned").touch()
    (abandoned / "read-only-artifact").write_text("stale", encoding="utf-8")
    (abandoned / "read-only-artifact").chmod(0o400)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    go_log = tmp_path / "go-list-cache"
    go_term_log = tmp_path / "go-term-handled"
    go_descendant_ready = tmp_path / "go-descendant-ready"
    go_descendant_hold = tmp_path / "go-descendant-hold"
    # Named pipes make both waits block in the kernel instead of spinning:
    # the descendant announces itself through one and is held by the other,
    # which never gets a writer, until the verifier kills its process group.
    os.mkfifo(go_descendant_ready)
    os.mkfifo(go_descendant_hold)
    fake_go = fake_bin / "go"
    fake_go.write_text(
        """#!/bin/sh
case " $* " in
  *" list -deps ./... "*)
    printf '%s\\n' "$GOCACHE" > "$FAKE_GO_LOG"
    if [ "${FAKE_GO_INTERRUPT:-}" = 1 ]; then
      (
        trap '' TERM
        printf 'ready\\n' > "$FAKE_GO_DESCENDANT_READY"
        IFS= read -r _held < "$FAKE_GO_HOLD"
      ) &
      descendant=$!
      trap 'printf "driver\\n" >> "$FAKE_GO_TERM_LOG"; exit 0' TERM
      IFS= read -r _ready < "$FAKE_GO_DESCENDANT_READY"
      kill -TERM "$PPID"
      kill -HUP "$PPID"
      wait "$descendant"
    fi
    ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    fake_go.chmod(0o755)
    setup = evaluator_go_module_setup_command(
        requirements,
        runtime_package_root=str(package_root),
        runtime_cache_root=str(cache_root),
    )
    environment = {
        **os.environ,
        "FAKE_GO_LOG": str(go_log),
        "FAKE_GO_TERM_LOG": str(go_term_log),
        "FAKE_GO_DESCENDANT_READY": str(go_descendant_ready),
        "FAKE_GO_HOLD": str(go_descendant_hold),
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TMPDIR": str(runtime_scratch),
    }

    interrupted = run_test_command(
        ["sh", "-c", setup],
        env={**environment, "FAKE_GO_INTERRUPT": "1"},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert interrupted.returncode != 0
    interrupted_diagnostic = interrupted.stdout + interrupted.stderr
    assert "evaluator Go module cache cannot resolve the declared package offline" in (
        interrupted_diagnostic
    )
    assert go_term_log.read_text(encoding="utf-8").splitlines() == ["driver"]
    assert go_log.is_file(), interrupted_diagnostic
    interrupted_cache = Path(go_log.read_text(encoding="utf-8").strip())
    assert not interrupted_cache.exists()
    assert not abandoned.exists()
    assert not tuple(verification_scratch.glob("cache.*"))

    recovered = run_test_command(
        ["sh", "-c", setup],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert recovered.returncode == 0, recovered.stdout + recovered.stderr
    assert not tuple(verification_scratch.glob("cache.*"))


@pytest.mark.native_build
def test_verification_cleanup_waits_for_recovery_and_accepts_already_removed_work(
    tmp_path: Path,
) -> None:
    """Recovery owns cleanup ordering and an already-clean tree is successful cleanup."""
    package_root = tmp_path / "package"
    package_root.mkdir()
    cache_root = tmp_path / "go-modules"
    cache_root.mkdir()
    runtime_scratch = tmp_path / "runtime-scratch"
    runtime_scratch.mkdir()
    module = EvaluatorGoModule(
        root=package_root,
        cache_root=cache_root,
        package_digest="sha256:" + "c" * 64,
    )
    requirements = TrustedEvaluatorRequirements(
        package_root=package_root,
        toolchains=frozenset({"go"}),
        go_module=module,
    )
    evaluator_go_module_ready_path(module, cache_root).mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_go = fake_bin / "go"
    fake_go.write_text(
        """#!/bin/sh
case " $* " in
  *" list -deps ./... "*)
    printf 'started\n' > "$GO_STARTED"
    IFS= read -r _release < "$RELEASE_GO"
    scratch=$(dirname -- "$GOCACHE")
    lease="$scratch/.lease.$(basename -- "$GOCACHE")"
    rm -rf -- "$GOCACHE"
    rm -f -- "$lease"
    printf 'removed\n' > "$WORK_REMOVED"
    ;;
esac
exit 0
""",
        encoding="utf-8",
    )
    fake_go.chmod(0o755)
    setup = evaluator_go_module_setup_command(
        requirements,
        runtime_package_root=str(package_root),
        runtime_cache_root=str(cache_root),
    )
    verification_scratch = runtime_scratch / f"vibesys-evaluator-go-verify-{os.getuid()}"
    lock_holder = tmp_path / "hold-recovery-lock.py"
    lock_holder.write_text(
        """import fcntl
import sys
from pathlib import Path

lock_path, held_fifo, release_fifo = map(Path, sys.argv[1:])
lock_path.parent.mkdir(parents=True, exist_ok=True)
with lock_path.open("a+b") as lock:
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
    held_fifo.write_text("held\\n", encoding="utf-8")
    release_fifo.read_text(encoding="utf-8")
""",
        encoding="utf-8",
    )
    go_started = tmp_path / "go-started"
    release_go = tmp_path / "release-go"
    work_removed = tmp_path / "work-removed"
    lock_held = tmp_path / "lock-held"
    release_lock = tmp_path / "release-lock"
    interleaving = f"""set -e
mkfifo -- {shlex.join(tuple(map(str, (go_started, release_go, work_removed, lock_held, release_lock))))}
(
{setup}
) &
owner=$!
IFS= read -r _started < {shlex.quote(str(go_started))}
{shlex.quote(sys.executable)} {shlex.quote(str(lock_holder))} \
  {shlex.quote(str(verification_scratch / ".recovery.lock"))} \
  {shlex.quote(str(lock_held))} {shlex.quote(str(release_lock))} &
recovery=$!
IFS= read -r _held < {shlex.quote(str(lock_held))}
printf 'release\n' > {shlex.quote(str(release_go))}
IFS= read -r _removed < {shlex.quote(str(work_removed))}
kill -0 "$owner"
printf 'release\n' > {shlex.quote(str(release_lock))}
wait "$recovery"
wait "$owner"
"""

    result = run_test_command(
        ["sh", "-c", interleaving],
        env={
            **os.environ,
            "GO_STARTED": str(go_started),
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "RELEASE_GO": str(release_go),
            "TMPDIR": str(runtime_scratch),
            "WORK_REMOVED": str(work_removed),
        },
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not tuple(verification_scratch.glob("cache.*"))
    assert not tuple(verification_scratch.glob(".lease.cache.*"))


@pytest.mark.native_build
def test_manifest_declared_go_gates_and_hotel_composition_run_from_prepared_offline_cache(
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> None:
    """An empty cache is populated during setup, then every gate is proxy-independent."""
    if shutil.which("go") is None:
        pytest.skip("Go is required to verify reusable evaluator packages")
    request.addfinalizer(lambda: _make_cache_tree_deletable(tmp_path))

    declared = _go_entrypoints()
    assert declared == {
        "microservice": ("servicebench", "otelinject", "otelcapture"),
        "ordered-map": ("vibesys-ordered-map",),
        "priority-queue": ("vibesys-priority-queue",),
        "queue": ("vibesys-queue",),
        "stack": ("vibesys-stack",),
        "unordered-map": ("vibesys-unordered-map",),
    }
    packages_root = BUNDLED_RESOURCES.directory("evaluators")
    assert packages_root is not None
    prepared_by_name = {}
    environment = {**os.environ, "GOCACHE": str(tmp_path / "go-build")}

    for package_name, entrypoints in declared.items():
        package = load_evaluator_package(packages_root / package_name)
        prepared = prepare_trusted_evaluator(
            package.root, tmp_path / "operator-cache" / package_name
        )
        module = prepared.requirements.go_module
        assert module is not None
        assert not module.cache_root.exists()
        setup = evaluator_go_module_setup_command(
            prepared.requirements,
            runtime_package_root=str(package.root),
            runtime_cache_root=str(module.cache_root),
        )
        setup_result = run_test_command(
            ["sh", "-c", setup],
            env=environment,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert setup_result.returncode == 0, setup_result.stdout + setup_result.stderr
        assert evaluator_go_module_ready_path(module, module.cache_root).is_dir()
        prepared_by_name[package_name] = (package, prepared)

        before = {
            path.relative_to(module.cache_root).as_posix(): path.read_bytes()
            for path in module.cache_root.rglob("*")
            if path.is_file()
        }
        for entrypoint in entrypoints:
            plan = prepare_trusted_evaluation_plan(
                TrustedEvaluationPlan(
                    accuracy_command=shlex.join(
                        package.command(
                            entrypoint,
                            *_successful_entrypoint_arguments(package_name, tmp_path),
                        )
                    )
                ),
                prepared.requirements,
                TrustedEvaluationCommandPaths(
                    source_project_root=tmp_path,
                    runtime_project_root=str(tmp_path),
                    python_executable="python3",
                    runtime_package_root=str(package.root),
                    runtime_go_module_cache_root=str(module.cache_root),
                ),
            )
            result = run_test_command(
                shlex.split(plan.accuracy_command or ""),
                env=environment,
                capture_output=True,
                text=True,
                timeout=180,
            )
            _assert_successful_offline(result)
        after = {
            path.relative_to(module.cache_root).as_posix(): path.read_bytes()
            for path in module.cache_root.rglob("*")
            if path.is_file()
        }
        assert after == before

    stack, stack_prepared = prepared_by_name["stack"]
    stack_module = stack_prepared.requirements.go_module
    assert stack_module is not None
    remote_stage = tmp_path / "remote-stage"
    staged_package = remote_stage / ".vibesys-evaluator-package"
    staged_cache = remote_stage / ".vibesys-evaluator-go-modules"
    shutil.copytree(stack.root, staged_package)
    shutil.copytree(stack_module.cache_root, staged_cache)
    remote = prepare_trusted_evaluation_plan(
        TrustedEvaluationPlan(
            accuracy_command=shlex.join(
                stack.command(
                    "vibesys-stack",
                    *_successful_entrypoint_arguments("stack", remote_stage),
                )
            )
        ),
        stack_prepared.requirements,
        TrustedEvaluationCommandPaths(
            source_project_root=tmp_path,
            runtime_project_root=".",
            python_executable="python3",
            runtime_package_root=staged_package.name,
            runtime_go_module_cache_root=staged_cache.name,
        ),
    )
    remote_result = run_test_command(
        shlex.split(remote.accuracy_command or ""),
        cwd=remote_stage,
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )
    _assert_successful_offline(remote_result)

    microservice, prepared = prepared_by_name["microservice"]
    module = prepared.requirements.go_module
    assert module is not None
    wrapper = (
        Path(__file__).parents[2]
        / "examples/microservices/hotel-correctness/.vibesys/tasks/compose/evaluator/run.py"
    )
    hotel = prepare_trusted_evaluation_plan(
        TrustedEvaluationPlan(
            accuracy_command=shlex.join(
                (
                    "python3",
                    str(wrapper),
                    "--package-root",
                    str(microservice.root),
                    "--",
                    "-h",
                )
            )
        ),
        prepared.requirements,
        TrustedEvaluationCommandPaths(
            source_project_root=tmp_path,
            runtime_project_root=str(tmp_path),
            python_executable="python3",
            runtime_package_root=str(microservice.root),
            runtime_go_module_cache_root=str(module.cache_root),
        ),
    )
    result = run_test_command(
        shlex.split(hotel.accuracy_command or ""),
        env=environment,
        capture_output=True,
        text=True,
        timeout=180,
    )
    _assert_successful_offline(result)

    _assert_incomplete_cache_is_rejected(
        stack.root,
        stack_module.cache_root,
        stack_prepared.requirements,
        environment,
        tmp_path,
    )
