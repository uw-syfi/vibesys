#!/usr/bin/env bash
# The checks test_install.sh runs inside the clean container, as an ordinary user:
#
#   test_install_in_container.sh install   the installer, with no git in the image
#   test_install_in_container.sh product  the installed release starting, checking and stopping runs
#
# Expects /opt/install.sh, the bundles of versions 1.0.0 and 1.0.1 in /bundles, the scripted agents'
# code in /opt/scripted, and $ARCH. The "product" phase expects git, which a task's repository needs.
#
# Every wait here is a bounded poll that guards against a hang; none sets a pace.
set -euo pipefail

home="$HOME/.vibesys"
bin="$HOME/.local/bin"
v1="/bundles/vibesys_1.0.0_linux_${ARCH}.tar.gz"
v2="/bundles/vibesys_1.0.1_linux_${ARCH}.tar.gz"
# state is where the orchestrator keeps its sessions when no setting names a place.
state="$HOME/.local/state/vibesys/orchestrator"

check() {
  echo "check: $*"
}
die() {
  echo "FAIL: $*" >&2
  exit 1
}
install() {
  VIBESYS_INSTALL_BUNDLE="$1" sh /opt/install.sh "${@:2}"
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

# host_process: the process of an installed release's host, if any. Only the check that nothing is
# left after a refused start looks at the process list; everything else asks the host.
host_process() { pgrep -f "^$home/releases/[^ ]*/bin/vibesys-host " || true; }
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
  check "nothing of the toolchain is here, and no git"
  for tool in go python3 uv git; do
    ! command -v "$tool" >/dev/null 2>&1 || die "$tool should not be installed in the clean container"
  done

  check "first install, with no git"
  install "$v1" >/tmp/install1.log 2>&1 || {
    cat /tmp/install1.log
    die "install failed"
  }
  cat /tmp/install1.log

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
  if vibesys-client release verify --installed "$home/current" >/tmp/verify.out 2>/tmp/verify.err; then die "a release without its job tool verified"; fi
  grep -q "bin/vibesys-cli" /tmp/verify.err || die "the missing file is not named: $(cat /tmp/verify.err)"
  mv "$home/vibesys-cli.moved" "$home/current/bin/vibesys-cli"
  vibesys-client release verify --installed "$home/current" >/dev/null || die "a repaired release does not verify"

  check "the install warned about PATH and confinement and gave the next steps"
  grep -q "unconfined" /tmp/install1.log || die "no warning about unconfined agents"
  grep -q "not on your PATH" /tmp/install1.log || die "no PATH hint"
  grep -q "vibesys-client run start" /tmp/install1.log || die "no next steps"

  check "re-install of the same version is a no-op"
  before="$(stat -c '%i %Y' "$home/releases/1.0.0" "$home/releases/1.0.0/sidecar/.venv/pyvenv.cfg")"
  install "$v1" >/tmp/install2.log 2>&1 || {
    cat /tmp/install2.log
    die "re-install failed"
  }
  after="$(stat -c '%i %Y' "$home/releases/1.0.0" "$home/releases/1.0.0/sidecar/.venv/pyvenv.cfg")"
  [ "$before" = "$after" ] || die "re-install changed the release"
  grep -q "already unpacked" /tmp/install2.log || die "re-install did not report the existing release"

  check "a corrupted bundle leaves the previous install in place"
  head -c 20000 "$v2" >/tmp/truncated_1.0.1.tar.gz
  if install /tmp/truncated_1.0.1.tar.gz >/tmp/install3.log 2>&1; then die "truncated bundle was accepted"; fi
  grep -q "failed while" /tmp/install3.log || die "failure did not name a step"
  cp "$v2" /tmp/flipped.tar.gz
  cp "$v2.sha256" /tmp/flipped.tar.gz.sha256
  printf 'x' | dd of=/tmp/flipped.tar.gz bs=1 seek=100 conv=notrunc 2>/dev/null
  if install /tmp/flipped.tar.gz >/tmp/install4.log 2>&1; then die "bundle with a wrong checksum was accepted"; fi
  grep -q "checksum" /tmp/install4.log || die "wrong checksum was not reported as such"
  [ "$(readlink "$home/current")" = "releases/1.0.0" ] || die "current moved after a failed install"
  [ ! -e "$home/releases/1.0.1" ] || die "a failed install left a release behind"
  ls -A "$home/releases" | grep -q '^\.stage' && die "a failed install left a staging directory"
  [ "$(vibesys-client --version)" = "vibesys-client 1.0.0" ]

  check "an Intel Mac is refused by name, and an Apple Silicon Mac under Rosetta gets arm64"
  # uname and sysctl are replaced by what a Mac prints. The download that follows an arm64 choice
  # has nowhere to go, so the failure names the asset that was chosen.
  mkdir -p /tmp/mac
  printf '#!/bin/sh\nif [ "$1" = -s ]; then echo Darwin; else echo x86_64; fi\n' >/tmp/mac/uname
  printf '#!/bin/sh\necho "${FAKE_ARM64:-0}"\n' >/tmp/mac/sysctl
  chmod +x /tmp/mac/uname /tmp/mac/sysctl
  if PATH="/tmp/mac:$PATH" FAKE_ARM64=0 VIBESYS_VERSION=9.9.9 VIBESYS_RELEASES_URL=http://127.0.0.1:9 \
    sh /opt/install.sh >/tmp/intel.log 2>&1; then die "an Intel Mac was accepted"; fi
  grep -q "do not cover Intel Macs" /tmp/intel.log || die "no clear Intel Mac message: $(cat /tmp/intel.log)"
  ! grep -q "cannot download" /tmp/intel.log || die "an Intel Mac got as far as a download"
  PATH="/tmp/mac:$PATH" FAKE_ARM64=1 VIBESYS_VERSION=9.9.9 VIBESYS_RELEASES_URL=http://127.0.0.1:9 \
    sh /opt/install.sh >/tmp/rosetta.log 2>&1 || true
  grep -q "vibesys_9.9.9_darwin_arm64.tar.gz" /tmp/rosetta.log || die "Rosetta did not choose arm64: $(cat /tmp/rosetta.log)"

  check "with no stable release published, the newest version cannot be found and the error says what to pass"
  if VIBESYS_RELEASES_URL=http://127.0.0.1:9 sh /opt/install.sh >/tmp/nolatest.log 2>&1; then die "an install with no version and no release was accepted"; fi
  grep -q "VIBESYS_VERSION=" /tmp/nolatest.log || die "the error does not tell the user to pass VIBESYS_VERSION: $(cat /tmp/nolatest.log)"

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
  sed -i "s|@COUNTER@|$counter|g" "$dir/.vibesys/tasks/toy/vibesys.input.toml"
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
no_leftovers() { [ -z "$(pgrep -f 'scripted_sidecar|vibesys-host mcp' || true)" ]; }

# run_pids: the sidecar and process-group pids the run's sessions recorded.
run_pids() {
  cat "$state"/sessions/*/*/sidecar.pid "$state"/sessions/*/*/agent.pgids 2>/dev/null | tr -s ' \n' '\n' | grep -E '^[0-9]+$' || true
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
  vibesys-client run start --task toy --detach >/tmp/noharness.out 2>/tmp/noharness.err || rc=$?
  cat /tmp/noharness.out /tmp/noharness.err
  [ "$rc" = 1 ] || die "start without a harness exited $rc, want 1"
  grep -q 'claude binary not found' /tmp/noharness.err || die "the failure does not name the missing harness (claude)"
  ! grep -q 'VIBESYS_ORCHESTRATOR_' /tmp/noharness.err /tmp/noharness.out || die "the failure asks for a VIBESYS_ORCHESTRATOR_ setting"
  host_answers || die "no vibesys-host of the release was started"
  host_release 1.0.0 || die "the host is not of the release that started it"
  stop_host

  check "a start the host refuses leaves no host, and shows only what caused this failure"
  printf 'an old line from another day\n' >>"$home/host.log"
  : >"$HOME/not-a-directory"
  vibesys-client host configure --state-directory "$HOME/not-a-directory/state" >/dev/null
  rc=0
  vibesys-client run list >/tmp/refused.out 2>/tmp/refused.err || rc=$?
  cat /tmp/refused.out /tmp/refused.err
  [ "$rc" = 1 ] || die "a refused start exited $rc, want 1"
  grep -q 'not-a-directory' /tmp/refused.err || die "the refusal does not name the state directory"
  ! grep -q 'an old line' /tmp/refused.err || die "the refusal shows an old log line"
  until_true 30 no_host_process || die "the refused start left a process: $(host_process)"
  no_host || die "the refused start left a host"
  vibesys-client host configure --clear >/dev/null

  check "the old settings do nothing: a host started with all of them set to nonsense ignores them"
  local stale="$HOME/nonsense"
  VIBESYS_ORCHESTRATOR_SIDECAR="$stale/sidecar" VIBESYS_ORCHESTRATOR_MARKETPLACE="$stale/marketplace" \
    VIBESYS_ORCHESTRATOR_STATE_DIR="$stale/state" VIBESYS_ORCHESTRATOR_CLI="$stale/cli" \
    vibesys-client run list >/tmp/hostile.out 2>/tmp/hostile.err || {
    cat /tmp/hostile.out /tmp/hostile.err
    die "a run list with old settings in the environment failed"
  }
  grep -q 'VIBESYS_ORCHESTRATOR_SIDECAR' /tmp/hostile.err || die "the client does not name the setting it ignores"
  host_answers || die "no host was started"
  ! vibesys-client host status --json | grep -q 'PART_ORIGIN_OPERATOR' || die "the host took a part from the environment"
  [ ! -e "$stale" ] || die "the host used a place the environment names"
  [ -d "$state" ] || die "the host's state is not in its usual place"
  stop_host

  check "VIBESYS_HOME that disagrees with the install is refused, printing the rule"
  rc=0
  VIBESYS_HOME="$HOME/elsewhere" vibesys-client host status >/tmp/rootrule.out 2>/tmp/rootrule.err || rc=$?
  [ "$rc" = 2 ] || die "a disagreeing VIBESYS_HOME exited $rc, want 2"
  grep -q 'the root is --root when given' /tmp/rootrule.err || die "the refusal does not print the rule: $(cat /tmp/rootrule.err)"
  [ ! -e "$HOME/elsewhere" ] || die "a refused VIBESYS_HOME was created"

  check "a run with scripted agents: start, leave, check, stop"
  # The host's configuration names the session holder: the scripted one, run by the release's own
  # Python. The host, client, vibesys-cli and marketplace are the release's. The scripted agents call
  # vibesys-cli by name, so a directory holding only that link is on the host's tool path.
  mkdir -p "$HOME/agent-path"
  ln -sf "$home/current/bin/vibesys-cli" "$HOME/agent-path/vibesys-cli"
  vibesys-client host configure \
    --session-holder "env PYTHONPATH=/opt/scripted VIBESYS_E2E_SCRIPT=act $py /opt/scripted/e2e/scripted_sidecar.py" \
    --tool-path "$HOME/agent-path:$PATH" >/dev/null

  local started run
  started="$(vibesys-client run start --task toy --detach --json)" || die "run start --detach failed"
  echo "$started"
  run="$("$py" -c 'import json,sys; print(json.load(sys.stdin)["runId"])' <<<"$started")"
  [ -n "$run" ] || die "run start printed no run id"

  vibesys-client run wait --until report --timeout 80s | tee /tmp/wait.out
  grep -q "report:" /tmp/wait.out || die "wait --until report did not show the report"

  vibesys-client run get --json >/tmp/get.json
  "$py" - /tmp/get.json "$run" <<'EOF' || die "get --json does not show the run, its agents and its report"
import json, sys
snapshot = json.load(open(sys.argv[1]))
runs, agents, messages = snapshot.get("runs", []), snapshot.get("agents", []), snapshot.get("messages", [])
assert [r["id"] for r in runs] == [sys.argv[2]], runs
assert len(agents) >= 2, agents
assert any("report" in m for m in messages), messages
EOF

  vibesys-client run list | tee /tmp/list.out
  grep -q "$run" /tmp/list.out || grep -q "${run:0:8}" /tmp/list.out || die "run list does not show the run"

  watched="$(run_pids)"
  [ -n "$watched" ] || die "a run with a report has no recorded sidecar"
  ! none_live || die "the run's recorded processes are not running"

  check "host status answers through a symlinked home, and the installer's stop is refused with a live run"
  host_answers || die "no host is running"
  # The same home reached through a link is the same host: one socket and one lock.
  ln -sfn "$HOME" "$HOME/home-link"
  vibesys-client --root "$HOME/home-link/.vibesys" host status | tee /tmp/status.out
  grep -q "live runs: 1" /tmp/status.out || die "host status through the link does not show the live run"
  if install "$v2" >/tmp/install5.log 2>&1; then die "switch was allowed while a run is live"; fi
  grep -q "live runs" /tmp/install5.log || die "refusal did not say why: $(cat /tmp/install5.log)"
  [ "$(readlink "$home/current")" = "releases/1.0.0" ] || die "current moved despite the refusal"
  host_answers || die "the refused install stopped the host"

  check "stop the run and nothing of it is left"
  vibesys-client run stop "$run" --yes
  vibesys-client run wait "$run" --until ended --timeout 80s
  until_true 30 none_live || die "processes of the run are still running"
  until_true 30 no_leftovers || die "a sidecar or tool server is left: $(pgrep -af 'scripted_sidecar|vibesys-host mcp')"

  check "with no live run the installer stops the idle host and switches, and the next host is the new release's"
  host_answers || die "the idle host is not running"
  install "$v2" >/tmp/install6.log 2>&1 || {
    cat /tmp/install6.log
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
  VIBESYS_HOME="$other" VIBESYS_INSTALL_DIR="$other-bin" install "$v1" >/tmp/install7.log 2>&1 || {
    cat /tmp/install7.log
    die "install into VIBESYS_HOME failed"
  }
  VIBESYS_HOME="$other" "$other-bin/vibesys-client" run list >/dev/null
  [ -S "$other/host.sock" ] || die "the host of VIBESYS_HOME does not serve at $other/host.sock"
  "$other-bin/vibesys-client" host status >/dev/null || die "the installed client does not find the host of its own root"
  no_host || die "the host of VIBESYS_HOME answers the other install's client"
  "$other-bin/vibesys-client" host stop

  echo "all product checks passed"
}

case "${1:-}" in
  install) phase_install ;;
  product) phase_product ;;
  *)
    echo "usage: $0 install|product" >&2
    exit 2
    ;;
esac
