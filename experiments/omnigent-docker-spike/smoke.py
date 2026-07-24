"""End-to-end spike: drive a VibeSys DockerSandboxLauncher against real Docker.

Run inside the 3.12 venv that has ``omnigent`` installed::

    python smoke.py

What it proves, against the *real* Omnigent ``SandboxLauncher`` ABC and a *real*
local Docker daemon (no mocks):

1. ``DockerSandboxLauncher`` is a concrete ``SandboxLauncher`` — zero abstract
   methods left, so Omnigent's managed-host flow would accept it.
2. ``ManagedSandboxConfig(launcher_factory=...)`` — Omnigent's documented
   embedding seam — accepts the launcher with no registry patch and no fork.
3. ``provision`` creates a real container; ``run`` execs in it; ``terminate``
   removes it.
4. The live VibeSys workspace is visible inside the container via bind mount,
   and ``materialize_workspace`` resolves to it *without* a git clone.
5. GPU/device resource args are threaded into the ``docker run`` argv (asserted
   on the argv, since this host has no GPU).

What it does NOT prove, and cannot on this host: a full agent turn (needs the
omnigent wheel baked into the image, a reachable server, real harness binaries,
and provider credentials); GPU passthrough executing on a GPU; mid-run GPU
reselect. Those are the residual unknowns recorded in the README.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from container import DockerContainerSandbox
from omnigent_launcher import DockerSandboxLauncher
from spec import BindMount, VibesysSandboxSpec

PASS = "PASS"
FAIL = "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((PASS if ok else FAIL, name, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    image = "python:3.12-bookworm"

    # 1. concrete against the real ABC
    from omnigent.onboarding.sandboxes.base import SandboxLauncher

    leftover = getattr(DockerSandboxLauncher, "__abstractmethods__", frozenset())
    check(
        "DockerSandboxLauncher is a concrete SandboxLauncher",
        issubclass(DockerSandboxLauncher, SandboxLauncher) and not leftover,
        f"unimplemented abstract methods: {sorted(leftover) or 'none'}",
    )

    # 2. the documented embedding seam accepts it
    workspace_host = Path(tempfile.mkdtemp(prefix="vibesys-ws-"))
    (workspace_host / "OBJECTIVE.md").write_text("optimize the kernel\n")
    spec = VibesysSandboxSpec(
        image=image,
        workspace=BindMount(source=workspace_host, target="/workspace"),
    )
    sandbox = DockerContainerSandbox()
    launcher = DockerSandboxLauncher(sandbox, spec)

    try:
        from omnigent.server.managed_hosts import ManagedSandboxConfig

        cfg = ManagedSandboxConfig(
            server_url="http://host.docker.internal:6767",
            launcher_factory=lambda: DockerSandboxLauncher(sandbox, spec),
            token_ttl_s=90000,
            provider="vibesys-docker",
        )
        produced = cfg.launcher_factory()
        check(
            "ManagedSandboxConfig(launcher_factory=...) accepts the launcher",
            isinstance(produced, SandboxLauncher),
            "no registry patch, no fork",
        )
    except Exception as exc:  # noqa: BLE001 — spike: any failure is a finding
        check("ManagedSandboxConfig accepts the launcher", False, repr(exc))

    # 5. GPU/device args threaded (argv assertion — no GPU on this host)
    gpu_spec = VibesysSandboxSpec(
        image=image,
        workspace=BindMount(source=workspace_host, target="/workspace"),
        gpus="device=0,1",
        devices=("/dev/nvidia0",),
        shm_size="16g",
    )
    argv = gpu_spec.docker_run_argv(name="probe")
    threaded = (
        "--gpus" in argv
        and argv[argv.index("--gpus") + 1] == "device=0,1"
        and "--device" in argv
        and "/dev/nvidia0" in argv
        and "--shm-size" in argv
    )
    check("GPU/device/shm resource args threaded into docker run argv", threaded, " ".join(argv))

    # 3 + 4. real container lifecycle through the launcher
    cid = None
    try:
        launcher.prepare()
        cid = launcher.provision("smoke")
        check("provision() created a real container", bool(cid), cid[:12] if cid else "")

        echo = launcher.run(cid, "echo container-live")
        check("run() execs inside the container", echo.returncode == 0 and "container-live" in echo.stdout)

        # the live workspace is visible via bind mount
        seen = launcher.run(cid, "cat /workspace/OBJECTIVE.md")
        check(
            "live host workspace visible inside container (bind mount)",
            "optimize the kernel" in seen.stdout,
            seen.stdout.strip(),
        )

        # materialize_workspace resolves to the mount, no clone
        stages: list[str] = []
        ws = launcher.materialize_workspace(
            cid,
            workspace="/workspace",
            repo_url="https://example.com/should-not-be-cloned.git",
            repo_branch=None,
            repo_name="repo",
            on_stage=stages.append,
        )
        no_clone = launcher.run(cid, "ls /workspace", check=False)
        check(
            "materialize_workspace resolves to bind mount without git clone",
            ws == "/workspace" and "repo" not in no_clone.stdout.split(),
            f"workdir={ws}, contents={no_clone.stdout.split()}",
        )

        # writes inside the container land on the host workspace
        launcher.run(cid, "echo 'result=42' > /workspace/RESULT.txt")
        wrote_back = (workspace_host / "RESULT.txt").exists()
        check("agent writes propagate back to the host workspace", wrote_back)

        alive = launcher.is_running(cid)
        check("is_running() reports the live container", alive is True)
    except Exception as exc:  # noqa: BLE001
        check("container lifecycle through the launcher", False, repr(exc))
    finally:
        if cid:
            launcher.terminate(cid)
            gone = launcher.is_running(cid)
            check("terminate() removes the container", gone is not True, f"is_running={gone}")

    print("\n--- summary ---")
    passed = sum(1 for r in results if r[0] == PASS)
    print(f"{passed}/{len(results)} checks passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
