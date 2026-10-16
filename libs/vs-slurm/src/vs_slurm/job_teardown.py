"""The job script's teardown: it ends every process the job started, and outlives them all.

The job script starts its service and its command each in a session of their own (with
``setsid``), lists each session id in the shell variable ``vs_groups``, and starts both with
:data:`JOB_PROCESS_MARKER` in their environment, set to a token unique to this run of the
script. When the script exits, for any reason, :func:`teardown_shell`'s ``vs_stop_groups``
sends SIGTERM to every live process of the job, gives them the stop grace, sends SIGKILL,
and then waits until none is left, up to the exit wait. The script exits only after that.

Why the script must outlive them: when a batch script exits, Slurm kills whatever is left in
the job step, and gives up after its UnkillableStepTimeout (60 s by default) with "Kill task
failed" and drains the node. A CUDA process that holds a large GPU allocation can take tens
of seconds to exit after SIGKILL, while the driver tears its memory down. So the script
waits for each process to really go, and when one outlives the wait it says which, on the
job's stderr, and fails the job with :data:`PROCESSES_OUTLIVED_TEARDOWN_EXIT_CODE`. A
service that never became ready keeps its own exit status, which says why the job failed.

The job's processes are those of its sessions, read from ``/proc/<pid>/stat``. A process
that left them (``setsid``, as Ray and daemons do) is found by the marker in its
environment, and its new session joins the job's, so its descendants are found too, and it
is still found after it starts exiting (when its environment is no longer readable). The
marker is set only on the commands the script starts, so the script itself and the helpers
it runs during teardown are never members.

A process counts as alive while it exists and is not a zombie whose last thread exited. A
zombie has released its memory and devices, and only its parent can reap it, which may be a
process outside the job. But a multithreaded process whose main thread exited shows as a
zombie while its other threads run, and a CUDA process releases the GPU only when its last
thread exits, so such a zombie (``nlwp`` above 1) is alive.

Not found: a descendant that leaves the job's sessions and also clears its environment
(``env -i setsid ...``), and a descendant that leaves them and is already exiting when the
teardown's first scan runs. Slurm still kills such a process when the script exits (with
``proctrack/cgroup`` it finds it by the job's cgroup), but the script does not wait for it.

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
# The environment variable that marks the processes the job script starts. Its value is
# unique to one run of the script, so another job's processes never match.
JOB_PROCESS_MARKER = "VS_SLURM_JOB_PROCESS"
_TERM_POLL_SECONDS = "0.2"
_KILL_POLL_SECONDS = "0.5"


def teardown_shell(
    *,
    stop_grace_seconds: int,
    exit_wait_seconds: int,
    status_path: PurePosixPath,
    kept_exit_code: int,
) -> list[str]:
    """Shell lines defining ``vs_stop_groups`` and installing it as the script's exit trap.

    After these lines, the script starts each command with :func:`start_in_own_session`.
    When the script is already exiting with ``kept_exit_code``, a process that outlives the
    wait is still named, but the job keeps that status.
    """
    status = shlex.quote(status_path.as_posix())
    outlived = PROCESSES_OUTLIVED_TEARDOWN_EXIT_CODE
    return [
        'vs_groups=""',
        'vs_alive=""',
        'vs_token="${SLURM_JOB_ID:-}.$$.$RANDOM$RANDOM"',
        # Adds the session of every marked process to vs_groups, then sets vs_alive to the
        # live processes of those sessions. Runs in the script's own shell, so the
        # sessions it learns persist; its own helpers (grep) run in the script's session,
        # which is never the job's. Fields after the command name in /proc/<pid>/stat:
        # $1 state, $4 session, $18 threads.
        "vs_scan() {",
        "  local file pid line own",
        # A marked command is in the script's own session for a moment, between its fork
        # and its setsid; that session (the script's process group too) is never the job's.
        "  read -r line < /proc/$$/stat",
        "  set -- ${line##*) }",
        "  own=$4",
        f'  for file in $(grep -lzxF -e "{JOB_PROCESS_MARKER}=$vs_token"'
        " /proc/[0-9]*/environ 2>/dev/null); do",
        '    read -r line 2>/dev/null < "${file%/environ}/stat" || continue',
        "    set -- ${line##*) }",
        '    case " $own 0 $vs_groups " in *" ${4:-0} "*) ;; *) vs_groups="$vs_groups $4" ;; esac',
        "  done",
        '  vs_alive=""',
        "  for file in /proc/[0-9]*/stat; do",
        "    pid=${file#/proc/}",
        "    pid=${pid%/stat}",
        '    [ "$pid" != "$$" ] || continue',
        '    read -r line 2>/dev/null < "$file" || continue',
        "    set -- ${line##*) }",
        # A zombie is gone only once its last thread is.
        '    [ "${1:-Z}" != Z ] || [ "${18:-1}" -gt 1 ] || continue',
        '    case " $vs_groups " in *" ${4:-0} "*) vs_alive="$vs_alive $pid" ;; esac',
        "  done",
        '  [ -n "$vs_alive" ]',
        "}",
        "vs_signal() {",
        "  local id",
        '  for id in $vs_groups; do kill -"$1" -- "-$id" 2>/dev/null; done',
        '  for id in $vs_alive; do kill -"$1" "$id" 2>/dev/null; done',
        "}",
        "vs_stop_groups() {",
        "  local status=$? deadline",
        '  [ -n "$vs_groups" ] || return 0',
        "  trap '' TERM",
        "  vs_scan",
        "  vs_signal TERM",
        f"  deadline=$((SECONDS + {stop_grace_seconds}))",
        f'  while vs_scan && [ "$SECONDS" -lt "$deadline" ]; do sleep {_TERM_POLL_SECONDS}; done',
        "  vs_signal KILL",
        f"  deadline=$((SECONDS + {exit_wait_seconds}))",
        '  while vs_scan && [ "$SECONDS" -lt "$deadline" ]; do',
        "    vs_signal KILL",
        f"    sleep {_KILL_POLL_SECONDS}",
        "  done",
        '  [ -n "$vs_alive" ] || return 0',
        f'  echo "vs-slurm: these processes of the job were still alive {exit_wait_seconds} s'
        ' after SIGKILL; Slurm may fail to end them and drain the node:" >&2',
        "  ps -o pid,ppid,pgid,sid,nlwp,stat,etime,args -p \"$(echo $vs_alive | tr ' ' ',')\" >&2",
        f'  [ "$status" -ne {kept_exit_code} ] || return 0',
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
    ``vs_groups`` so teardown ends the whole session. The command, and every process it
    starts that keeps its environment, carries the job's marker, so teardown finds such a
    process even after it left the session.
    """
    suffix = f" {redirect}" if redirect else ""
    return [
        f'{JOB_PROCESS_MARKER}="$vs_token" setsid {command}{suffix} &',
        f"{pid_variable}=$!",
        f'vs_groups="$vs_groups ${pid_variable}"',
    ]
