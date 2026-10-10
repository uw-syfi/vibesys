"""A real Slurm cluster in Docker, for the ``slurm_cluster`` test tier.

Two containers from one image form the cluster: ``head`` (munge, slurmctld, and
sshd, so it is both the controller and the login node) and ``node`` (munge and
slurmd, the one compute node, privileged so bubblewrap can mount a procfs). A directory of the host is mounted into both at its own
absolute path and stands in for the shared filesystem.

Everything here talks to Docker and Slurm through their command-line clients, as
an operator would. Containers and the network are created with names and labels
unique to one :class:`SlurmCluster`, and teardown removes exactly those.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pwd
import secrets
import shutil
import sys
import tempfile
import textwrap
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from tests.support import run_test_command

if TYPE_CHECKING:
    import subprocess
    from collections.abc import Callable, Iterator, Sequence

IMAGE_DIR = Path(__file__).parent / "image"
#: Marks every Docker object this tier creates, with the cluster's own id as value.
CLUSTER_LABEL = "io.vibesys.slurm-cluster-test"
#: The node name in ``image/slurm.conf``, and the address names the head and node answer to.
NODE_NAME = "node1"
HEAD_NAME = "slurmhead"
#: How long a condition on the cluster may stay false before the test is declared hung.
HANG_GUARD_SECONDS = 120.0
_POLL_SECONDS = 0.05


class ClusterUnavailableError(RuntimeError):
    """The host cannot run the cluster; the message says why."""


def docker(
    *argv: str, check: bool = True, timeout: float = 600
) -> subprocess.CompletedProcess[str]:
    """Run the ``docker`` CLI with *argv* and capture its text output."""
    return run_test_command(
        ("docker", *argv), text=True, capture_output=True, check=check, timeout=timeout
    )


def docker_problem() -> str | None:
    """Return why the tier cannot run here, or ``None`` when Docker is usable."""
    if shutil.which("docker") is None:
        return "docker is not installed"
    info = docker("info", "--format", "{{json .Runtimes}}", check=False, timeout=60)
    if info.returncode != 0:
        return "the Docker daemon is not reachable"
    return None


def wait_until(condition: Callable[[], bool], *, what: str) -> None:
    """Block until *condition* holds; fail with *what* if it stays false past the hang guard.

    Used only for conditions on another process (a job reaching the queue, a
    job leaving it). The interval is not a timing assumption; the guard only
    turns a hang into a failure.
    """
    deadline = time.monotonic() + HANG_GUARD_SECONDS
    stop = threading.Event()
    while not condition():
        # test-isolation: the deadline only guards a hang; a satisfied condition returns at once
        if time.monotonic() > deadline:
            message = f"timed out waiting for {what}"
            raise TimeoutError(message)
        stop.wait(_POLL_SECONDS)


def build_image(*, dockerfile: str, tag_prefix: str, build_args: dict[str, str]) -> str:
    """Build (or reuse, through Docker's cache) an image from ``image/``; return its tag.

    The tag is a digest of the Dockerfile's directory and the build arguments, so
    a changed input builds a new image and an unchanged one costs a cache check.
    """
    digest = hashlib.sha256()
    for path in sorted(IMAGE_DIR.iterdir()):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    digest.update(json.dumps(build_args, sort_keys=True).encode())
    tag = f"{tag_prefix}:{digest.hexdigest()[:16]}"
    args = [item for key, value in build_args.items() for item in ("--build-arg", f"{key}={value}")]
    docker(
        "build", "-q", "--tag", tag, "--file", str(IMAGE_DIR / dockerfile), *args, str(IMAGE_DIR)
    )
    return tag


@dataclass(frozen=True)
class SlurmCluster:
    """A started cluster: its containers, its shared directory, and how to reach it."""

    cluster_id: str
    root: Path
    head: str
    node: str
    network: str
    user: str
    uid: int
    gid: int
    ssh_port: int
    ssh_key: Path
    #: A short, private directory for the SSH control socket (socket paths are length-limited).
    control_dir: Path

    # -- running programs in the cluster ------------------------------------------------

    def run(
        self, container: str, argv: Sequence[str], *, check: bool = True, as_root: bool = False
    ) -> subprocess.CompletedProcess[str]:
        """Run *argv* in *container* as the Slurm user (or root) and capture its output."""
        user = "root" if as_root else str(self.uid)
        return docker("exec", "-u", user, container, *argv, check=check)

    def slurm(self, *argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        """Run a Slurm client program (``squeue``, ``scancel``, ...) on the head node."""
        return self.run(self.head, argv, check=check)

    # -- the queue --------------------------------------------------------------------

    def queue(self) -> list[tuple[str, str, str]]:
        """Return ``(job id, job name, state)`` of every job Slurm still tracks as active."""
        out = self.slurm("squeue", "-h", "-o", "%i|%j|%T").stdout
        rows = [line.split("|") for line in out.splitlines() if line]
        return [(job, name, state) for job, name, state in rows]

    def jobs_named(self, prefix: str) -> list[str]:
        """Return the ids of active jobs whose name starts with *prefix*."""
        return [job for job, name, _ in self.queue() if name.startswith(prefix)]

    def wait_for_job(self, prefix: str) -> str:
        """Block until a job named *prefix...* is running; return its id."""
        found: list[str] = []

        def running() -> bool:
            found[:] = [
                job
                for job, name, state in self.queue()
                if name.startswith(prefix) and state == "RUNNING"
            ]
            return bool(found)

        wait_until(running, what=f"a running job named {prefix}*")
        return found[0]

    def wait_for_empty_queue(self) -> None:
        """Block until no job is pending, running, or completing."""
        wait_until(lambda: not self.queue(), what="an empty queue")

    def job_state(self, job_id: str) -> str:
        """Return the final state of *job_id*, as ``scontrol`` keeps it for MinJobAge."""
        out = self.slurm("scontrol", "-o", "show", "job", job_id).stdout
        return next(
            field.split("=", 1)[1] for field in out.split() if field.startswith("JobState=")
        )

    def cancel_all(self) -> None:
        """Cancel every job of the Slurm user; test cleanup, not a test step."""
        self.slurm("scancel", "-u", self.user, check=False)

    # -- how the host reaches the cluster ---------------------------------------------

    def ssh_command(self) -> tuple[str, ...]:
        """Return the ``ssh`` argv (no host) for the ``slurm`` environment's SSH transport.

        Everything the connection needs is on the command line, so nothing in the
        user's SSH configuration or ``known_hosts`` is read or written.
        """
        return (
            "ssh",
            "-F", "none",
            "-i", str(self.ssh_key),
            "-p", str(self.ssh_port),
            "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=no",
            "-o", "UserKnownHostsFile=/dev/null",
            "-o", "LogLevel=ERROR",
            # One connection per session: a gate makes dozens of short ssh calls.
            "-o", "ControlMaster=auto",
            "-o", f"ControlPath={self.control_dir}/%C",
            "-o", "ControlPersist=60",
        )  # fmt: skip

    def ssh_host(self) -> str:
        """Return the ``user@address`` the SSH transport connects to."""
        return f"{self.user}@127.0.0.1"

    def write_login_shim(self, directory: Path) -> Path:
        """Write a program that runs a Slurm client as it would run on a login node.

        ``shim srun ARGS`` runs ``srun ARGS`` on the head container, which is the
        login node, with this process's working directory (for srun) and exactly this
        process's environment (``env -i``), as the Slurm user. It is the
        ``srun_command`` and ``scancel_command`` of the ``slurm-gpu`` operator
        configuration, so the host-side client needs no Slurm installation.
        Standard streams and the exit status pass through ``docker exec``.
        """
        shim = directory / "login-node"
        shim.write_text(
            textwrap.dedent(
                f"""\
                #!{sys.executable}
                import os, sys
                program, *arguments = sys.argv[1:]
                environment = [f"{{key}}={{value}}" for key, value in os.environ.items()]
                # Only srun starts something in the caller's directory (the workspace,
                # which is mounted at this path on the head). scancel runs wherever
                # the caller happens to be, and that path need not exist there.
                directory = ["-w", os.getcwd()] if program == "srun" else []
                os.execv(
                    {shutil.which("docker")!r},
                    ["docker", "exec", "-i", "-u", "{self.uid}", *directory,
                     {self.head!r}, "env", "-i", *environment,
                     "/usr/bin/" + program, *arguments],
                )
                """
            ),
            encoding="utf-8",
        )
        shim.chmod(0o755)
        return shim

    def stop(self) -> None:
        """Remove exactly the containers and network this cluster created."""
        if self.ssh_port:
            run_test_command(
                (*self.ssh_command(), "-O", "exit", self.ssh_host()), capture_output=True
            )
        shutil.rmtree(self.control_dir, ignore_errors=True)
        docker("rm", "-f", "-v", self.head, self.node, check=False)
        docker("network", "rm", self.network, check=False)


def start_cluster(root: Path, *, image: str) -> SlurmCluster:
    """Start a cluster whose shared directory is *root*, and return once its node is idle.

    *root* is mounted into both containers at its own absolute path.
    """
    cluster_id = secrets.token_hex(4)
    root.mkdir(parents=True, exist_ok=True)
    secrets_dir = root.parent / f"{root.name}-secrets-{cluster_id}"
    secrets_dir.mkdir(mode=0o700)
    munge_key = secrets_dir / "munge.key"
    munge_key.write_bytes(os.urandom(1024))
    ssh_key = secrets_dir / "id_ed25519"
    run_test_command(
        ("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(ssh_key)), check=True
    )
    shutil.copy(f"{ssh_key}.pub", secrets_dir / "authorized_keys")

    uid, gid = os.getuid(), os.getgid()
    head, node, network = (f"vs-slurm-{role}-{cluster_id}" for role in ("head", "node", "net"))
    label = f"{CLUSTER_LABEL}={cluster_id}"
    control_dir = Path(tempfile.mkdtemp(prefix="vs-ssh-"))
    cluster = SlurmCluster(
        cluster_id, root, head, node, network, _user_name(uid), uid, gid, 0, ssh_key, control_dir
    )
    try:
        docker("network", "create", "--label", label, network)
        common = ("--detach", "--init", "--network", network, "--label", label, "-e", f"CLUSTER_UID={uid}",
                  "-v", f"{munge_key}:/run/cluster/munge.key:ro", "-v", f"{root}:{root}")  # fmt: skip
        docker("run", *common, "--name", head, "--hostname", HEAD_NAME, "--network-alias", HEAD_NAME,
                "-p", "127.0.0.1::22", "-v", f"{secrets_dir / 'authorized_keys'}:/run/cluster/authorized_keys:ro",
                image, "head")  # fmt: skip
        docker("run", *common, "--name", node, "--hostname", NODE_NAME, "--network-alias", NODE_NAME,
                "--privileged", image, "node")  # fmt: skip
        port = docker("port", head, "22/tcp").stdout.split(":")[-1].strip()
        cluster = dataclasses.replace(cluster, ssh_port=int(port))
        # Blocks inside the head container until the node registered as idle.
        cluster.run(head, ("timeout", str(int(HANG_GUARD_SECONDS)), "bash", "-c",
                           "until sinfo -h -t idle | grep -q . && sacctmgr -n list cluster | grep -q .; do sleep 0.1; done"))  # fmt: skip
    except BaseException:
        cluster.stop()
        raise
    return cluster


def _user_name(uid: int) -> str:
    return pwd.getpwuid(uid).pw_name


def containers_labelled(cluster_id: str) -> Iterator[str]:
    """Yield the ids of the containers this cluster started (for leak checks)."""
    out = docker("ps", "-aq", "--filter", f"label={CLUSTER_LABEL}={cluster_id}").stdout
    yield from out.split()
