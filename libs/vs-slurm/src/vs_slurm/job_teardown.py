"""The job script's teardown: it ends every process the job started, and outlives them all.

The job script starts its service and its command each in a session of their own (with
``setsid``), and lists each session id in the shell variable ``vs_groups``. When the script
exits, for any reason, :func:`teardown_shell`'s ``vs_stop_groups`` sends SIGTERM to every
live process of those sessions, gives them the stop grace, sends SIGKILL, and then waits
until none is left, up to the exit wait. The script exits only after that.

Why the script must outlive them: when a batch script exits, Slurm kills whatever is left in
the job step, and gives up after its UnkillableStepTimeout (60 s by default) with "Kill task
failed" and drains the node. A CUDA process that holds a large GPU allocation can take tens
of seconds to exit after SIGKILL, while the driver tears its memory down. So the script
waits for each process to really go, and when one outlives the wait it says which, on the
job's stderr, and fails the job with :data:`PROCESSES_OUTLIVED_TEARDOWN_EXIT_CODE`.

A process counts as alive while it exists and is not a zombie: a zombie has released its
memory and devices, and only its parent can reap it, which may be a process outside the job.
A process is found by session as well as by process group, so a worker that moved into a
process group of its own (``timeout(1)`` does that) is still found. Membership is read from
``ps``; where ``ps`` cannot list sessions, the script falls back to probing the process
group with ``kill -0``, which also counts zombies.

Slurm's own SIGTERM (a cancel or the time limit) runs the same teardown; Slurm sends SIGKILL
to the script KillWait (30 s by default) later, which ends the wait early, so the stop grace
stays under KillWait (``service_stop_grace_seconds`` is at most 20). Once teardown has
begun, a further SIGTERM no longer cuts it short.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import PurePosixPath

# The job's own exit status when a process it started was still alive (and no zombie) the
# configured exit wait after SIGKILL. The job script writes it and names the processes.
PROCESSES_OUTLIVED_TEARDOWN_EXIT_CODE = 71
_TERM_POLL_SECONDS = "0.2"
_KILL_POLL_SECONDS = "0.5"


def teardown_shell(
    *, stop_grace_seconds: int, exit_wait_seconds: int, status_path: PurePosixPath
) -> list[str]:
    """Shell lines defining ``vs_stop_groups`` and installing it as the script's exit trap.

    After these lines, the script appends each session id it starts to ``vs_groups``.
    """
    status = shlex.quote(status_path.as_posix())
    outlived = PROCESSES_OUTLIVED_TEARDOWN_EXIT_CODE
    return [
        'vs_groups=""',
        # The pids of the live (non-zombie) processes in session or process group $1.
        "vs_members() {",
        "  local table",
        '  if ! table=$(ps -e -o pid=,sid=,pgid=,stat= 2>/dev/null) || [ -z "$table" ]; then',
        '    if kill -0 -- "-$1" 2>/dev/null; then printf \'%s\\n\' "$1"; fi',
        "    return 0",
        "  fi",
        "  printf '%s\\n' \"$table\""
        " | awk -v id=\"$1\" '($2 == id || $3 == id) && $4 !~ /^Z/ { print $1 }'",
        "}",
        "vs_live() {",
        "  local id",
        '  for id in $vs_groups; do [ -z "$(vs_members "$id")" ] || return 0; done',
        "  return 1",
        "}",
        "vs_signal() {",
        "  local id pid",
        "  for id in $vs_groups; do",
        '    kill -"$1" -- "-$id" 2>/dev/null',
        '    for pid in $(vs_members "$id"); do kill -"$1" "$pid" 2>/dev/null; done',
        "  done",
        "}",
        "vs_stop_groups() {",
        '  [ -n "$vs_groups" ] || return 0',
        "  trap '' TERM",
        "  vs_signal TERM",
        f"  local deadline=$((SECONDS + {stop_grace_seconds})) pids id",
        f'  while vs_live && [ "$SECONDS" -lt "$deadline" ]; do sleep {_TERM_POLL_SECONDS}; done',
        "  vs_signal KILL",
        f"  deadline=$((SECONDS + {exit_wait_seconds}))",
        f'  while vs_live && [ "$SECONDS" -lt "$deadline" ]; do sleep {_KILL_POLL_SECONDS}; done',
        "  vs_live || return 0",
        '  pids=""',
        '  for id in $vs_groups; do pids="$pids $(vs_members "$id")"; done',
        f'  echo "vs-slurm: these processes of the job were still alive {exit_wait_seconds} s'
        ' after SIGKILL; Slurm may fail to end them and drain the node:" >&2',
        "  ps -o pid,ppid,pgid,sid,stat,etime,args -p \"$(echo $pids | tr ' ' ',')\" >&2",
        f"  printf '%s\\n' {outlived} > {status}",
        f"  exit {outlived}",
        "}",
        "trap vs_stop_groups EXIT",
        # Slurm's SIGTERM at a cancel or time limit runs the teardown above too.
        "trap 'exit 143' TERM",
    ]


def start_in_own_session(command: str, *, pid_variable: str, redirect: str = "") -> list[str]:
    """Shell lines that start ``command`` in the background, in a session of its own.

    The session id is the new process's pid, stored in ``pid_variable`` and appended to
    ``vs_groups`` so teardown ends the whole session.
    """
    suffix = f" {redirect}" if redirect else ""
    return [
        f"setsid {command}{suffix} &",
        f"{pid_variable}=$!",
        f'vs_groups="$vs_groups ${pid_variable}"',
    ]
