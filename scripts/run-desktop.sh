#!/usr/bin/env bash
# Open the VibeSys web UI in an Electron window, against a local gateway or one
# running on an SSH host. See "Desktop app" in docs/contributing/web-development.md.
set -euo pipefail

repository_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repository_root"

usage() {
  cat >&2 <<'USAGE'
usage: scripts/run-desktop.sh (--project PATH | --demo) [--port N] [-- RUN_ARGS...]
       scripts/run-desktop.sh --remote USER@HOST --remote-repo PATH (--project PATH | --demo)
                              [--port N] [-- RUN_ARGS...]

With --remote, PATH values are paths on the remote host (quote '~/proj' so your
shell does not expand it locally), and key-based SSH authentication is required.
The local and remote port are the same: the gateway checks the browser Origin.
USAGE
}

die() {
  echo "run-desktop: $*" >&2
  exit 1
}

remote="" remote_repo="" port=8765 project="" demo=0
run_args=()
while (($#)); do
  case "$1" in
    --remote) remote="${2:?--remote needs USER@HOST}"; shift 2 ;;
    --remote-repo) remote_repo="${2:?--remote-repo needs a path}"; shift 2 ;;
    --port) port="${2:?--port needs a number}"; shift 2 ;;
    --project) project="${2:?--project needs a path}"; shift 2 ;;
    --demo) demo=1; shift ;;
    -h | --help) usage; exit 0 ;;
    --) shift; run_args=("$@"); break ;;
    *) usage; die "unknown argument: $1" ;;
  esac
done

[[ "$port" =~ ^[0-9]+$ ]] && ((port >= 1 && port <= 65535)) || die "--port must be 1-65535"
if ((demo)) && [[ -n "$project" ]]; then die "pass --project or --demo, not both"; fi
if ((!demo)) && [[ -z "$project" ]]; then usage; die "pass --project or --demo"; fi
if ((demo)) && ((${#run_args[@]})); then die "--demo does not accept run arguments"; fi
if [[ -n "$remote" && -z "$remote_repo" ]]; then die "--remote needs --remote-repo"; fi
if [[ -z "$remote" && -n "$remote_repo" ]]; then die "--remote-repo needs --remote"; fi

for tool in pnpm uv; do
  command -v "$tool" >/dev/null 2>&1 || die "$tool is required; install $tool and try again"
done
[[ -z "$remote" ]] || command -v ssh >/dev/null 2>&1 || die "ssh is required for --remote"

pnpm --dir clients install --frozen-lockfile

web_args=(live --port "$port" --no-open)
if ((demo)); then web_args+=(--demo); else web_args+=(--project "$project"); fi
if ((${#run_args[@]})); then web_args+=(-- "${run_args[@]}"); fi

# Quote a remote path for the remote shell, keeping a leading ~/ expandable there.
remote_path() {
  if [[ "$1" == "~/"* ]]; then
    printf '"$HOME"/%q' "${1#\~/}"
  else
    printf '%q' "$1"
  fi
}

quoted_args=""
for arg in "${web_args[@]}"; do quoted_args+=" $(printf '%q' "$arg")"; done

control_dir="" control_socket="" tunnel_up=0
ssh_options=(-o BatchMode=yes -o ConnectTimeout=15)

cleanup() {
  if ((tunnel_up)); then
    ssh -S "$control_socket" -O exit "$remote" >/dev/null 2>&1 || true
  fi
  [[ -z "$control_dir" ]] || rm -rf "$control_dir"
}
trap cleanup EXIT

# Run the gateway helper, echo its output without the capability token, and set
# `gateway_url` and `instance_record` from the lines it prints.
gateway_url="" instance_record=""
start_gateway() {
  local line status=""
  while IFS= read -r line; do
    case "$line" in
      "VibeSys web UI ready: "*) gateway_url="${line#VibeSys web UI ready: }" ;;
      "Instance record: "*) instance_record="${line#Instance record: }"; echo "$line" ;;
      __run_desktop_status__=*) status="${line#__run_desktop_status__=}" ;;
      *token=*) ;;
      *) echo "$line" ;;
    esac
  done < <("$@" 2>&1 </dev/null; echo "__run_desktop_status__=$?")
  [[ "$status" == 0 ]] || die "starting the gateway failed (exit ${status:-unknown})"
  [[ -n "$gateway_url" ]] || die "the gateway did not print a 'VibeSys web UI ready' URL"
}

if [[ -z "$remote" ]]; then
  start_gateway uv run python -m entrypoints.web "${web_args[@]}"
  stop_command="cd $(printf '%q' "$repository_root") && uv run python -m entrypoints.web stop --instance $(printf '%q' "$instance_record")"
else
  if (echo >"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
    die "local port $port is already in use; pick another with --port (it is also used on $remote)"
  fi
  remote_command="cd -- $(remote_path "$remote_repo") && pnpm --dir clients install --frozen-lockfile && uv run python -m entrypoints.web$quoted_args"
  start_gateway ssh "${ssh_options[@]}" "$remote" "bash -lc $(printf '%q' "$remote_command")"

  control_dir="$(mktemp -d "${TMPDIR:-/tmp}/vibesys-ssh.XXXXXX")"
  control_socket="$control_dir/control"
  ssh -M -S "$control_socket" -fN "${ssh_options[@]}" -o ExitOnForwardFailure=yes \
    -L "$port:127.0.0.1:$port" "$remote" \
    || die "could not open the SSH tunnel to $remote (key-based auth is required)"
  tunnel_up=1
  stop_command="ssh $remote 'cd -- $(remote_path "$remote_repo") && uv run python -m entrypoints.web stop --instance $(printf '%q' "$instance_record")'"
fi

# The gateway checks the exact browser Origin, so the window must use the port
# the gateway was started on, which the tunnel mirrors.
[[ "$gateway_url" == "http://127.0.0.1:$port/"* ]] \
  || die "the gateway URL does not use port $port; refusing to open it"

echo "Opening the VibeSys desktop window (close it to exit)."
desktop_status=0
# The URL carries the capability token, so it travels in the environment, not argv.
VIBESYS_DESKTOP_URL="$gateway_url" pnpm --dir clients --filter @vibesys/desktop start || desktop_status=$?

echo
echo "The gateway is still running. Stop it with:"
echo "  $stop_command"
exit "$desktop_status"
