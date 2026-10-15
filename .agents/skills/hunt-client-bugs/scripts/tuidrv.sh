#!/usr/bin/env bash
# tuidrv.sh — drive the VibeSys TUI headlessly in a detached tmux session so an
# agent can launch it, read rendered frames, send keystrokes, resize, and read
# backend logs. OpenTUI needs a real PTY; tmux provides one. Nothing here needs
# a human at a terminal.
#
# Usage:
#   tuidrv.sh start <COLSxROWS> <PROJECT> <TASK> [extra vibesys args...]
#   tuidrv.sh cap                 # rendered frame, plain text (layout/wrapping)
#   tuidrv.sh capE                # rendered frame WITH ansi escapes (color checks)
#   tuidrv.sh keys <k> [k...]     # send named keys/chords: Enter Escape C-w C-l F4 ...
#   tuidrv.sh type <text...>      # send literal text (e.g. a slash command body)
#   tuidrv.sh cmd  <text...>      # type text then Enter (e.g. cmd /help)
#   tuidrv.sh size <COLSxROWS>    # resize the window to test responsive layout
#   tuidrv.sh log  [N]            # tail N lines of the live session's backend.log
#   tuidrv.sh wait [SECS]         # sleep, for renders/agent turns to settle
#   tuidrv.sh stop                # kill the tmux session
#
# Env overrides: VIBESYS_REPO, VS_TUI_SESSION, VIBESYS_PYTHON.
set -uo pipefail

REPO="${VIBESYS_REPO:-/home/ayanbrafaih/VibeSys}"
SESS="${VS_TUI_SESSION:-vsbug}"
export PATH="$HOME/.local/bin:$HOME/.bun/bin:$HOME/.local/go/bin:$HOME/.cargo/bin:$PATH"
export VIBESYS_PYTHON="${VIBESYS_PYTHON:-$REPO/.venv/bin/python}"
export GOTOOLCHAIN=local

sub="${1:-help}"; shift || true
case "$sub" in
  start)
    size="$1"; project="$2"; task="$3"; shift 3
    cols="${size%x*}"; rows="${size#*x}"
    tmux kill-session -t "$SESS" 2>/dev/null || true
    tmux new-session -d -s "$SESS" -x "$cols" -y "$rows"
    tmux set-option -t "$SESS" window-size manual 2>/dev/null || true
    tmux send-keys -t "$SESS" \
      "cd '$REPO' && export PATH='$PATH' VIBESYS_PYTHON='$VIBESYS_PYTHON' GOTOOLCHAIN=local && node clients/tui/dist/launcher.js --project '$project' --task '$task' $* 2>&1" Enter
    echo "started session '$SESS' (${cols}x${rows}) project=$project task=$task args=$*"
    ;;
  cap)  tmux capture-pane -t "$SESS" -p ;;
  capE) tmux capture-pane -t "$SESS" -e -p ;;
  keys) tmux send-keys -t "$SESS" "$@" ;;
  type) tmux send-keys -t "$SESS" -l "$*" ;;
  cmd)  tmux send-keys -t "$SESS" -l "$*"; tmux send-keys -t "$SESS" Enter ;;
  size) tmux resize-window -t "$SESS" -x "${1%x*}" -y "${1#*x}"; echo "resized to $1" ;;
  log)  f="$(ls -t /tmp/vibesys-session-*/backend.log 2>/dev/null | head -1)"; [ -n "$f" ] && { echo "== $f =="; tail -n "${1:-40}" "$f"; } || echo "no live backend.log" ;;
  wait) sleep "${1:-3}" ;;
  stop) tmux kill-session -t "$SESS" 2>/dev/null && echo "stopped '$SESS'" || echo "no session '$SESS'" ;;
  *) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//' ;;
esac
