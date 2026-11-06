#!/usr/bin/env bash
# The checks test_install.sh runs inside the clean container, and test_install_macos.sh runs on a
# Mac, as an ordinary user:
#
#   test_install_in_container.sh install   the installer, with no git in the image
#   test_install_in_container.sh product   the installed release starting, checking and stopping runs
#   test_install_in_container.sh client    a person's verbs on a run: attach, steer, a killed host, a killed session
#   test_install_in_container.sh macos     what only a Mac has: signatures, Gatekeeper and socket path limits
#
# In the container it expects /opt/install.sh, the bundles of versions 1.0.0 and 1.0.1 in /bundles,
# the scripted agents' code in /opt/scripted, and $ARCH. The "product" and "client" phases expect
# git, which a task's repository needs. On a Mac the caller names those places with INSTALL_SH,
# BUNDLES_DIR, SCRIPTED_DIR, OS and SCRATCH, and sets CLEAN_MACHINE=0 because the machine has a
# toolchain of its own.
#
# Every wait here is a bounded poll that guards against a hang; none sets a pace.
set -euo pipefail

home="$HOME/.vibesys"
bin="$HOME/.local/bin"
install_sh="${INSTALL_SH:-/opt/install.sh}"
scripted="${SCRIPTED_DIR:-/opt/scripted}"
os="${OS:-linux}"
v1="${BUNDLES_DIR:-/bundles}/vibesys_1.0.0_${os}_${ARCH}.tar.gz"
v2="${BUNDLES_DIR:-/bundles}/vibesys_1.0.1_${os}_${ARCH}.tar.gz"
# scratch holds this run's own logs, which are not the product's.
scratch="${SCRATCH:-/tmp}"
# state is where the orchestrator keeps its sessions when no setting names a place.
state="$HOME/.local/state/vibesys/orchestrator"

check() {
  echo "check: $*"
}
die() {
  echo "FAIL: $*" >&2
  diagnose >&2
  exit 1
}

# diagnose: what the host and the scripted agents wrote, for whoever reads the log of a failure.
diagnose() {
  local file
  echo "--- the end of the host's log"
  tail -n 40 "$home/host.log" 2>/dev/null || true
  for file in "$state"/sessions/*/*/peer-failure.txt; do
    [ -f "$file" ] || continue
    echo "--- $file"
    cat "$file"
  done
}
install() {
  VIBESYS_INSTALL_BUNDLE="$1" sh "$install_sh" "${@:2}"
}

# inode_and_mtime PATH...: each path's inode and modification time. GNU stat takes -c and BSD stat
# (macOS) takes -f; each rejects the other's flag.
inode_and_mtime() {
  stat -c '%i %Y' "$@" 2>/dev/null || stat -f '%i %m' "$@"
}

# until_true SECONDS COMMAND...: poll COMMAND until it succeeds, failing after SECONDS (a bound
# against a hang, not a pace).
until_true() {
  local limit="$1"
  shift
  local waited=0
  until "$@"; do
    waited=$((waited + 1))
    [ "$waited" -le $((limit * 5)) ] || return 1
    sleep 0.2
  done
}

# physical DIR: DIR with its symlinks resolved. A process's command line holds the path the program
# was started by after the system resolved it: on a Mac /tmp is /private/tmp.
physical() { (cd "$1" 2>/dev/null && pwd -P) || echo "$1"; }

# host_process: the process of an installed release's host, if any. Only the check that nothing is
# left after a refused start looks at the process list; everything else asks the host.
host_process() { pgrep -f "^($home|$(physical "$home"))/releases/[^ ]*/bin/vibesys-host " || true; }
no_host_process() { [ -z "$(host_process)" ]; }

# host_answers: a host serves this root, asked the way every client asks.
host_answers() { vibesys-client host status >/dev/null 2>&1; }

# no_host: the client finds nobody serving, as against a host it cannot reach.
no_host() {
  local out
  out="$(vibesys-client host status 2>&1)" && return 1
  [[ "$out" == *"no host is running"* ]]
}

# host_release VERSION: the host that answers is of that release.
host_release() {
  vibesys-client host status --json | grep -Eq "\"release\": *\"$1\""
}

# stop_host: stop the host. `host stop` returns once nobody serves.
stop_host() {
  vibesys-client host stop
  no_host || die "a stopped host still answers"
}

phase_install() {
  if [ "${CLEAN_MACHINE:-1}" = 1 ]; then
    check "nothing of the toolchain is here, and no git"
    for tool in go python3 uv git; do
      ! command -v "$tool" >/dev/null 2>&1 || die "$tool should not be installed in the clean container"
    done
  fi

  check "first install, with no git"
  install "$v1" >$scratch/install1.log 2>&1 || {
    cat $scratch/install1.log
    die "install failed"
  }
  cat $scratch/install1.log

  check "layout"
  for f in bin/vibesys-client bin/vibesys-host bin/vibesys-cli sidecar/pyproject.toml sidecar/uv.lock \
    sidecar/README.md sidecar/src/vs_sidecar/__init__.py sidecar/.venv/bin/python \
    marketplace/.claude-plugin/marketplace.json marketplace/skills/README.md release.json; do
    [ -e "$home/releases/1.0.0/$f" ] || die "missing $home/releases/1.0.0/$f"
  done
  [ "$(readlink "$home/current")" = "releases/1.0.0" ] || die "current does not point at releases/1.0.0"
  [ "$(readlink "$bin/vibesys-client")" = "$home/current/bin/vibesys-client" ] || die "vibesys-client link is wrong"

  check "only vibesys-client is on PATH"
  export PATH="$bin:$PATH"
  command -v vibesys-client >/dev/null || die "vibesys-client is not on PATH"
  ! command -v vibesys-host >/dev/null 2>&1 || die "vibesys-host must not be on PATH"
  ! command -v vibesys-cli >/dev/null 2>&1 || die "vibesys-cli must not be on PATH"

  check "every binary reports the bundle's version"
  [ "$(vibesys-client --version)" = "vibesys-client 1.0.0" ] || die "vibesys-client --version: $(vibesys-client --version)"
  [ "$("$home/current/bin/vibesys-host" --version)" = "vibesys-host 1.0.0" ] || die "vibesys-host --version"
  "$home/current/bin/vibesys-cli" --version | grep -q '^vibesys-cli 1\.0\.0 ' || die "vibesys-cli --version: $("$home/current/bin/vibesys-cli" --version)"

  check "the sidecar imports"
  "$home/current/sidecar/.venv/bin/python" -c 'import vs_sidecar'
  check "the release verifies itself, and says its version"
  [ "$(vibesys-client release verify --installed "$home/current")" = "1.0.0" ] || die "release verify did not print the version"
  # A part gone from an installed release is named, and put back it verifies again.
  mv "$home/current/bin/vibesys-cli" "$home/vibesys-cli.moved"
  if vibesys-client release verify --installed "$home/current" >$scratch/verify.out 2>$scratch/verify.err; then die "a release without its job tool verified"; fi
  grep -q "bin/vibesys-cli" $scratch/verify.err || die "the missing file is not named: $(cat $scratch/verify.err)"
  mv "$home/vibesys-cli.moved" "$home/current/bin/vibesys-cli"
  vibesys-client release verify --installed "$home/current" >/dev/null || die "a repaired release does not verify"

  check "the install warned about PATH and confinement and gave the next steps"
  grep -q "unconfined" $scratch/install1.log || die "no warning about unconfined agents"
  grep -q "not on your PATH" $scratch/install1.log || die "no PATH hint"
  grep -q "vibesys-client run start" $scratch/install1.log || die "no next steps"

  check "re-install of the same version is a no-op"
  before="$(inode_and_mtime "$home/releases/1.0.0" "$home/releases/1.0.0/sidecar/.venv/pyvenv.cfg")"
  install "$v1" >$scratch/install2.log 2>&1 || {
    cat $scratch/install2.log
    die "re-install failed"
  }
  after="$(inode_and_mtime "$home/releases/1.0.0" "$home/releases/1.0.0/sidecar/.venv/pyvenv.cfg")"
  [ "$before" = "$after" ] || die "re-install changed the release"
  grep -q "already unpacked" $scratch/install2.log || die "re-install did not report the existing release"

  check "a corrupted bundle leaves the previous install in place"
  head -c 20000 "$v2" >$scratch/truncated_1.0.1.tar.gz
  if install $scratch/truncated_1.0.1.tar.gz >$scratch/install3.log 2>&1; then die "truncated bundle was accepted"; fi
  grep -q "failed while" $scratch/install3.log || die "failure did not name a step"
  cp "$v2" $scratch/flipped.tar.gz
  cp "$v2.sha256" $scratch/flipped.tar.gz.sha256
  printf 'x' | dd of=$scratch/flipped.tar.gz bs=1 seek=100 conv=notrunc 2>/dev/null
  if install $scratch/flipped.tar.gz >$scratch/install4.log 2>&1; then die "bundle with a wrong checksum was accepted"; fi
  grep -q "checksum" $scratch/install4.log || die "wrong checksum was not reported as such"
  [ "$(readlink "$home/current")" = "releases/1.0.0" ] || die "current moved after a failed install"
  [ ! -e "$home/releases/1.0.1" ] || die "a failed install left a release behind"
  ls -A "$home/releases" | grep -q '^\.stage' && die "a failed install left a staging directory"
  [ "$(vibesys-client --version)" = "vibesys-client 1.0.0" ]

  check "an Intel Mac is refused by name, and an Apple Silicon Mac under Rosetta gets arm64"
  # uname and sysctl are replaced by what a Mac prints. The download that follows an arm64 choice
  # has nowhere to go, so the failure names the asset that was chosen.
  mkdir -p $scratch/mac
  printf '#!/bin/sh\nif [ "$1" = -s ]; then echo Darwin; else echo x86_64; fi\n' >$scratch/mac/uname
  printf '#!/bin/sh\necho "${FAKE_ARM64:-0}"\n' >$scratch/mac/sysctl
  chmod +x $scratch/mac/uname $scratch/mac/sysctl
  if PATH="$scratch/mac:$PATH" FAKE_ARM64=0 VIBESYS_VERSION=9.9.9 VIBESYS_RELEASES_URL=http://127.0.0.1:9 \
    sh "$install_sh" >$scratch/intel.log 2>&1; then die "an Intel Mac was accepted"; fi
  grep -q "do not cover Intel Macs" $scratch/intel.log || die "no clear Intel Mac message: $(cat $scratch/intel.log)"
  ! grep -q "cannot download" $scratch/intel.log || die "an Intel Mac got as far as a download"
  PATH="$scratch/mac:$PATH" FAKE_ARM64=1 VIBESYS_VERSION=9.9.9 VIBESYS_RELEASES_URL=http://127.0.0.1:9 \
    sh "$install_sh" >$scratch/rosetta.log 2>&1 || true
  grep -q "vibesys_9.9.9_darwin_arm64.tar.gz" $scratch/rosetta.log || die "Rosetta did not choose arm64: $(cat $scratch/rosetta.log)"

  check "with no stable release published, the newest version cannot be found and the error says what to pass"
  if VIBESYS_RELEASES_URL=http://127.0.0.1:9 sh "$install_sh" >$scratch/nolatest.log 2>&1; then die "an install with no version and no release was accepted"; fi
  grep -q "VIBESYS_VERSION=" $scratch/nolatest.log || die "the error does not tell the user to pass VIBESYS_VERSION: $(cat $scratch/nolatest.log)"

  echo "all install checks passed"
}

# toy_repository DIR: a git repository holding the task the scripted agents optimize, the same as
# gateway/e2e's: one file, score.txt, and a benchmark that reports it. The benchmark adds a
# hundredth for each sample in turn (+0.00, +0.01, +0.02), counted in a file outside the checkout,
# because identical samples make vibesys-cli's compare inconclusive.
toy_repository() {
  local dir="$1" counter="$HOME/toy-samples"
  : >"$counter"
  mkdir -p "$dir/.vibesys/tasks/toy"
  printf '10\n' >"$dir/score.txt"
  cat >"$dir/.vibesys/tasks/toy/vibesys.input.toml" <<'EOF'
version = "0.1"
backends = ["cpu"]

[accuracy]
command = ['sh', '-c', 'test -s ${PROJECT_ROOT}/score.txt']

[benchmark]
command = ['sh', '-c', 'n=$(wc -c < @COUNTER@); printf x >> @COUNTER@; printf "{\"score\": %s.0%s}" "$(cat ${PROJECT_ROOT}/score.txt)" "$((n % 3))" > "$2"', 'sh']

[benchmark.result]
json_argument = "--out"
metric = "score"
EOF
  # sed -i takes a backup suffix on BSD (macOS) and none on GNU, so the edit goes through a copy.
  sed "s|@COUNTER@|$counter|g" "$dir/.vibesys/tasks/toy/vibesys.input.toml" >"$dir/.vibesys/tasks/toy/vibesys.input.toml.new"
  mv "$dir/.vibesys/tasks/toy/vibesys.input.toml.new" "$dir/.vibesys/tasks/toy/vibesys.input.toml"
  printf '[[objective]]\nname = "score"\ndirection = "max"\n' >"$dir/.vibesys/tasks/toy/objectives.toml"
  git -C "$dir" init -q -b main
  git -C "$dir" add score.txt .vibesys
  git -C "$dir" -c user.name=toy -c user.email=toy@example.com -c commit.gpgsign=false commit -q -m "toy task"
  # vibesys-cli promotes by moving this branch, which no one may have checked out.
  git -C "$dir" branch vibesys/result/toy "$(git -C "$dir" rev-parse HEAD)"
}

# live PIDS...: print those of the pids that are running.
live() {
  local pid
  for pid in "$@"; do
    ! kill -0 "$pid" 2>/dev/null || echo "$pid"
  done
}

# watched holds the pids none_live looks at.
watched=""
none_live() {
  # shellcheck disable=SC2086 # a list of pids
  [ -z "$(live $watched)" ]
}
leftover_pattern() { echo "($scripted|$(physical "$scripted"))/e2e/scripted_sidecar|($home|$(physical "$home"))/releases/[^ ]*/bin/vibesys-host mcp"; }
no_leftovers() { [ -z "$(pgrep -f "$(leftover_pattern)" || true)" ]; }

# run_pids: the sidecar and process-group pids the run's sessions recorded in their SessionProcesses.
run_pids() {
  "$home/current/sidecar/.venv/bin/python" - "$state"/sessions/*/*/processes.pb <<'PY' || true
import sys
from vs_sidecar.gen.vibesys.orchestrator.v1 import agent_session_pb2 as sc

for path in sys.argv[1:]:
    try:
        record = sc.SessionProcesses.FromString(open(path, "rb").read())
    except OSError:
        continue
    print(record.holder_pid, *(g.id for g in record.agent_process_groups), sep="\n")
PY
}

# configure_scripted_holder MODE [stand-in]: the host's session holder is the scripted one, in the
# mode MODE (act or reply). With "stand-in" each session also gets a real process in a group of its
# own, standing in for the agent. The host must not be running, and starts with this the next time.
configure_scripted_holder() {
  local py="$home/current/sidecar/.venv/bin/python" stand_in=""
  [ "${2:-}" != stand-in ] || stand_in="VIBESYS_E2E_STAND_IN_AGENT=1"
  mkdir -p "$HOME/agent-path"
  ln -sf "$home/current/bin/vibesys-cli" "$HOME/agent-path/vibesys-cli"
  vibesys-client host configure \
    --session-holder "env PYTHONPATH=$scripted VIBESYS_E2E_SCRIPT=$1 $stand_in $py $scripted/e2e/scripted_sidecar.py" \
    --tool-path "$HOME/agent-path:$PATH" >/dev/null
}

# start_run: start a run of the toy task, leave it, and print its id.
start_run() {
  local started run
  started="$(vibesys-client run start --task toy --detach --json)" || die "run start --detach failed"
  echo "$started" >&2
  run="$("$home/current/sidecar/.venv/bin/python" -c 'import json,sys; print(json.load(sys.stdin)["runId"])' <<<"$started")"
  [ -n "$run" ] || die "run start printed no run id"
  echo "$run"
}

phase_product() {
  export PATH="$bin:$PATH"
  local repo="$HOME/repo" py="$home/current/sidecar/.venv/bin/python"
  [ "$(vibesys-client --version)" = "vibesys-client 1.0.0" ]
  toy_repository "$repo"
  cd "$repo"

  check "with no setting, the release finds its own parts and a run fails for the missing harness"
  # Nothing is set: no VIBESYS_ variable, no harness installed. The client starts the host from the
  # release, which launches the release's own sidecar; the sidecar finds no agent program.
  env | grep '^VIBESYS_' && die "a VIBESYS_ variable is set in the test environment"
  command -v claude >/dev/null 2>&1 && die "claude should not be installed in the clean container"
  command -v codex >/dev/null 2>&1 && die "codex should not be installed in the clean container"
  local rc=0
  vibesys-client run start --task toy --detach >$scratch/noharness.out 2>$scratch/noharness.err || rc=$?
  cat $scratch/noharness.out $scratch/noharness.err
  [ "$rc" = 1 ] || die "start without a harness exited $rc, want 1"
  grep -q 'claude binary not found' $scratch/noharness.err || die "the failure does not name the missing harness (claude)"
  ! grep -q 'VIBESYS_ORCHESTRATOR_' $scratch/noharness.err $scratch/noharness.out || die "the failure asks for a VIBESYS_ORCHESTRATOR_ setting"
  host_answers || die "no vibesys-host of the release was started"
  host_release 1.0.0 || die "the host is not of the release that started it"
  stop_host

  check "a start the host refuses leaves no host, and shows only what caused this failure"
  printf 'an old line from another day\n' >>"$home/host.log"
  : >"$HOME/not-a-directory"
  vibesys-client host configure --state-directory "$HOME/not-a-directory/state" >/dev/null
  rc=0
  vibesys-client run list >$scratch/refused.out 2>$scratch/refused.err || rc=$?
  cat $scratch/refused.out $scratch/refused.err
  [ "$rc" = 1 ] || die "a refused start exited $rc, want 1"
  grep -q 'not-a-directory' $scratch/refused.err || die "the refusal does not name the state directory"
  ! grep -q 'an old line' $scratch/refused.err || die "the refusal shows an old log line"
  until_true 30 no_host_process || die "the refused start left a process: $(host_process)"
  no_host || die "the refused start left a host"
  vibesys-client host configure --clear >/dev/null

  check "the old settings do nothing: a host started with all of them set to nonsense ignores them"
  local stale="$HOME/nonsense"
  VIBESYS_ORCHESTRATOR_SIDECAR="$stale/sidecar" VIBESYS_ORCHESTRATOR_MARKETPLACE="$stale/marketplace" \
    VIBESYS_ORCHESTRATOR_STATE_DIR="$stale/state" VIBESYS_ORCHESTRATOR_CLI="$stale/cli" \
    vibesys-client run list >$scratch/hostile.out 2>$scratch/hostile.err || {
    cat $scratch/hostile.out $scratch/hostile.err
    die "a run list with old settings in the environment failed"
  }
  grep -q 'VIBESYS_ORCHESTRATOR_SIDECAR' $scratch/hostile.err || die "the client does not name the setting it ignores"
  host_answers || die "no host was started"
  ! vibesys-client host status --json | grep -q 'PART_ORIGIN_OPERATOR' || die "the host took a part from the environment"
  [ ! -e "$stale" ] || die "the host used a place the environment names"
  [ -d "$state" ] || die "the host's state is not in its usual place"
  stop_host

  check "VIBESYS_HOME that disagrees with the install is refused, printing the rule"
  rc=0
  VIBESYS_HOME="$HOME/elsewhere" vibesys-client host status >$scratch/rootrule.out 2>$scratch/rootrule.err || rc=$?
  [ "$rc" = 2 ] || die "a disagreeing VIBESYS_HOME exited $rc, want 2"
  grep -q 'the root is --root when given' $scratch/rootrule.err || die "the refusal does not print the rule: $(cat $scratch/rootrule.err)"
  [ ! -e "$HOME/elsewhere" ] || die "a refused VIBESYS_HOME was created"

  check "a run with scripted agents: start, leave, check, stop"
  # The host's configuration names the session holder: the scripted one, run by the release's own
  # Python. The host, client, vibesys-cli and marketplace are the release's. The scripted agents call
  # vibesys-cli by name, so a directory holding only that link is on the host's tool path.
  configure_scripted_holder act

  local run
  run="$(start_run)"

  vibesys-client run wait --until report --timeout 80s | tee $scratch/wait.out
  grep -q "final report 1:" $scratch/wait.out || die "wait --until report did not show the report"

  vibesys-client run get --json >$scratch/get.json
  "$py" - $scratch/get.json "$run" <<'EOF' || die "get --json does not show the run, its agents and its report"
import json, sys
snapshot = json.load(open(sys.argv[1]))
runs, agents, messages = snapshot.get("runs", []), snapshot.get("agents", []), snapshot.get("messages", [])
assert [r["id"] for r in runs] == [sys.argv[2]], runs
assert len(agents) >= 2, agents
assert any("report" in m for m in messages), messages
EOF

  vibesys-client run list | tee $scratch/list.out
  grep -q "$run" $scratch/list.out || grep -q "${run:0:8}" $scratch/list.out || die "run list does not show the run"

  watched="$(run_pids)"
  [ -n "$watched" ] || die "a run with a report has no recorded sidecar"
  ! none_live || die "the run's recorded processes are not running"

  check "host status answers through a symlinked home, and the installer's stop is refused with a live run"
  host_answers || die "no host is running"
  # The same home reached through a link is the same host: one socket and one lock.
  ln -sfn "$HOME" "$HOME/home-link"
  vibesys-client --root "$HOME/home-link/.vibesys" host status | tee $scratch/status.out
  grep -q "live runs: 1" $scratch/status.out || die "host status through the link does not show the live run"
  if install "$v2" >$scratch/install5.log 2>&1; then die "switch was allowed while a run is live"; fi
  grep -q "live runs" $scratch/install5.log || die "refusal did not say why: $(cat $scratch/install5.log)"
  [ "$(readlink "$home/current")" = "releases/1.0.0" ] || die "current moved despite the refusal"
  host_answers || die "the refused install stopped the host"

  check "stop the run and nothing of it is left"
  vibesys-client run stop "$run" --yes
  vibesys-client run wait "$run" --until ended --timeout 80s
  until_true 30 none_live || die "processes of the run are still running"
  until_true 30 no_leftovers || die "a sidecar or tool server is left: $(pgrep -af "$(leftover_pattern)")"

  check "with no live run the installer stops the idle host and switches, and the next host is the new release's"
  host_answers || die "the idle host is not running"
  install "$v2" >$scratch/install6.log 2>&1 || {
    cat $scratch/install6.log
    die "install of the new release failed with an idle host"
  }
  [ "$(readlink "$home/current")" = "releases/1.0.1" ] || die "the install did not switch"
  [ -d "$home/releases/1.0.0" ] || die "the old release was removed"
  no_host || die "the installer left the old host running"
  [ "$(vibesys-client --version)" = "vibesys-client 1.0.1" ] || die "the client is not the new release"
  vibesys-client run list >/dev/null
  host_answers || die "no host started from the new release"
  host_release 1.0.1 || die "the new host is not of release 1.0.1"
  stop_host

  check "VIBESYS_HOME moves the install and its host together, set or not"
  local other="$HOME/other"
  VIBESYS_HOME="$other" VIBESYS_INSTALL_DIR="$other-bin" install "$v1" >$scratch/install7.log 2>&1 || {
    cat $scratch/install7.log
    die "install into VIBESYS_HOME failed"
  }
  VIBESYS_HOME="$other" "$other-bin/vibesys-client" run list >/dev/null
  [ -S "$other/host.sock" ] || die "the host of VIBESYS_HOME does not serve at $other/host.sock"
  "$other-bin/vibesys-client" host status >/dev/null || die "the installed client does not find the host of its own root"
  no_host || die "the host of VIBESYS_HOME answers the other install's client"
  "$other-bin/vibesys-client" host stop

  echo "all product checks passed"
}

# snapshot_says WHAT: the run's snapshot, as `run get --json` prints it, answers WHAT, a Python
# expression over `snapshot`. It is false when the host does not answer.
snapshot_says() {
  vibesys-client run get --json 2>/dev/null |
    "$home/current/sidecar/.venv/bin/python" -c "import json, sys; snapshot = json.load(sys.stdin); sys.exit(0 if ($1) else 1)" 2>/dev/null
}

# completed_turns N: the run has completed at least N agent turns.
completed_turns() {
  snapshot_says "len([t for t in snapshot.get('turns', []) if t.get('status') == 'TURN_STATUS_COMPLETED']) >= $1"
}

# live_session: print the pid of a session process that is running and the process groups it
# recorded for its agent, if it recorded any.
live_session() {
  "$home/current/sidecar/.venv/bin/python" - "$state"/sessions/*/*/processes.pb <<'PY' || true
import os
import sys

from vs_sidecar.gen.vibesys.orchestrator.v1 import agent_session_pb2 as sc

for path in sys.argv[1:]:
    try:
        record = sc.SessionProcesses.FromString(open(path, "rb").read())
        os.kill(record.holder_pid, 0)
    except OSError:
        continue
    groups = [g.id for g in record.agent_process_groups]
    if groups:
        print(record.holder_pid, *groups)
        break
PY
}
has_live_session() { [ -n "$(live_session)" ]; }

phase_client() {
  export PATH="$bin:$PATH"
  local repo="$HOME/repo-client" py="$home/current/sidecar/.venv/bin/python"
  toy_repository "$repo"
  cd "$repo"
  vibesys-client host stop >/dev/null 2>&1 || true
  no_host || die "a host is still serving"

  check "start a run with scripted agents that only reply, and leave it"
  configure_scripted_holder reply stand-in
  local run
  run="$(start_run)"
  until_true 60 completed_turns 1 || die "the lead never completed a turn"

  check "come back: get shows the run, and attach follows it until it is interrupted"
  snapshot_says "[r['id'] for r in snapshot.get('runs', [])] == ['$run']" || die "get --json does not show the run"
  vibesys-client run get >"$scratch/get.txt"
  grep -q "${run:0:8}" "$scratch/get.txt" || die "get does not show the run: $(cat "$scratch/get.txt")"
  vibesys-client run attach "$run" >"$scratch/attach.out" 2>"$scratch/attach.err" &
  local attach=$! rc=0
  until_true 30 grep -q "attached to run" "$scratch/attach.err" || {
    kill "$attach" 2>/dev/null || true
    die "attach did not attach: $(cat "$scratch/attach.err")"
  }
  kill -INT "$attach"
  wait "$attach" || rc=$?
  [ "$rc" = 0 ] || die "an interrupted attach exited $rc, want 0: $(cat "$scratch/attach.err")"
  grep -q "detached: run" "$scratch/attach.err" || die "the interrupt did not detach: $(cat "$scratch/attach.err")"
  snapshot_says "[r['status'] for r in snapshot['runs']] == ['RUN_STATUS_RUNNING']" || die "detaching stopped the run"

  check "steer the lead, and it takes another turn"
  local said
  said="$(vibesys-client run steer "$run" "say it again")"
  echo "$said"
  [[ "$said" == *"new turn"* || "$said" == *"has it"* || "$said" == *"sent into"* || "$said" == *"queued"* ]] || die "steer said: $said"
  until_true 60 completed_turns 2 || die "the steered lead never completed its second turn"

  check "a host killed without warning is replaced, and finds the run and its session process still there"
  local before holder
  until_true 30 has_live_session || die "no session process recorded a process group for its agent"
  before="$(live_session)"
  holder="${before%% *}"
  [ -n "$(host_process)" ] || die "the host's process is not found by its command line: $(ps -axo pid=,command= | grep vibesys-host | grep -v grep)"
  kill -KILL "$(host_process)"
  until_true 30 no_host_process || die "the killed host is still running"
  snapshot_says "[r['status'] for r in snapshot['runs']] == ['RUN_STATUS_RUNNING']" || die "the run did not survive the host: $(vibesys-client run get 2>&1)"
  host_answers || die "no new host answered"
  kill -0 "$holder" || die "the session process $holder died with the host"
  vibesys-client run steer "$run" "once more" >/dev/null
  until_true 60 completed_turns 3 || die "the lead did not answer after the host was replaced"
  [ "$(live_session)" = "$before" ] || die "the session process or its agent's groups changed: was $before, now $(live_session)"

  check "a session process killed without warning ends its session lost, with why, and its agent's processes are reaped"
  # The host ends the lost session's agent and starts the agent's next session at once, so the
  # reason is in the run's history as it happens: attach, started before the kill, shows it.
  watched="${before#* }"
  vibesys-client run attach "$run" >"$scratch/lost.out" 2>&1 &
  local follow=$!
  until_true 30 grep -q "attached to run" "$scratch/lost.out" || die "attach did not attach: $(cat "$scratch/lost.out")"
  kill -KILL "$holder"
  until_true 60 grep -q "no longer running" "$scratch/lost.out" || {
    kill -INT "$follow" 2>/dev/null || true
    die "the session was not lost, with why: $(cat "$scratch/lost.out")"
  }
  kill -INT "$follow"
  wait "$follow" || true
  until_true 30 none_live || die "the agent's process groups are still running: $(live $watched)"

  check "stop the run and nothing of it is left"
  vibesys-client run stop "$run" --yes
  vibesys-client run wait "$run" --until ended --timeout 80s
  until_true 30 no_leftovers || die "a session process or tool server is left: $(pgrep -af "$(leftover_pattern)")"
  stop_host
  echo "all client checks passed"
}

# phase_macos: what only a Mac has. Output is kept in the log as evidence of what a user meets.
phase_macos() {
  export PATH="$bin:$PATH"
  local release="$home/current" binary
  check "what the Mac makes of the binaries a curl install leaves"
  for binary in vibesys-client vibesys-host vibesys-cli; do
    echo "--- $binary"
    file "$release/bin/$binary"
    codesign -dv "$release/bin/$binary" 2>&1 || true
    xattr -l "$release/bin/$binary" || true
    spctl --assess --type execute -vv "$release/bin/$binary" 2>&1 || true
  done
  echo "--- the macOS version"
  sw_vers

  check "a binary a browser downloaded (marked quarantined) is run by its first user"
  # curl and tar leave no mark, so the installer's own files are not quarantined. A bundle fetched in
  # a browser has the mark on the archive, and a file the Archive Utility unpacks from it inherits it.
  local marked="$scratch/quarantined-client" rc_marked=0
  cp "$release/bin/vibesys-client" "$marked"
  xattr -w com.apple.quarantine "0081;$(printf '%x' "$(date +%s)");Safari;" "$marked"
  xattr -l "$marked"
  "$marked" --version || rc_marked=$?
  echo "a quarantined, ad-hoc signed binary run from a shell exited $rc_marked"

  check "a root whose socket paths do not fit in a Unix socket address is refused by name, and a short state directory fixes it"
  # A Unix socket address holds 104 bytes on macOS. The user temp directory under /var/folders is
  # already about 50 of them, so a root made there is as long as a person's can get.
  local long rc=0 client
  long="$(mktemp -d)/vibesys-root-with-a-long-name"
  echo "the root is ${#long} bytes long: $long"
  VIBESYS_HOME="$long" VIBESYS_INSTALL_DIR="$long-bin" install "$v1" >"$scratch/long-install.log" 2>&1 || {
    cat "$scratch/long-install.log"
    die "install into a long root failed"
  }
  client="$long-bin/vibesys-client"
  (cd "$HOME/repo-client" && "$client" run start --task toy --detach >"$scratch/long.out" 2>"$scratch/long.err") || rc=$?
  cat "$scratch/long.out" "$scratch/long.err"
  [ "$rc" != 0 ] || die "a state directory whose socket does not fit was accepted"
  grep -q "bytes" "$scratch/long.err" || die "the refusal does not say what is too long"
  grep -q "state-directory" "$scratch/long.err" || die "the refusal does not say what to do"
  "$client" host stop >/dev/null 2>&1 || true
  "$client" host configure --state-directory "$scratch/long-state" >/dev/null
  "$client" run list >/dev/null || die "a host with a short state directory did not start in a long root"
  "$client" host stop
  echo "macos checks done"
}

case "${1:-}" in
  install) phase_install ;;
  product) phase_product ;;
  client) phase_client ;;
  macos) phase_macos ;;
  *)
    echo "usage: $0 install|product|client|macos" >&2
    exit 2
    ;;
esac
