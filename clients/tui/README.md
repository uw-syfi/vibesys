# VibeSys TUI

Terminal client and launcher for VibeSys.

```bash
npm install -g @vibesys/tui
vs --help
```

The package installs `vs` and `vibesys` as aliases for the same launcher. The
launcher starts the Python VibeSys backend with `python -m vibesys --headless`
and then attaches the OpenTUI client. Install the Python `vibesys` package in
the Python environment you want to use, or set `VIBESYS_PYTHON` to that Python
executable.

## Operator interface

Enter ordinary text to ask the supervision backend about the current run. The
available slash commands are:

| Command | Behavior |
| --- | --- |
| `/help` | Show commands and planned controls. |
| `/history` | List rounds with agent-active elapsed time. |
| `/perf` | Plot the recorded performance metric by round. |

The footer shows keyboard navigation. `[` and `]` select rounds, Tab and
Shift+Tab select agents, Page Up/Page Down scroll the transcript, Ctrl+T expands
todos, Ctrl+P expands the latest prompt in the current selection, Ctrl+L returns
to the live view, and Ctrl+C exits. Commands listed under "Planned" in `/help`
are not accepted yet.

The launcher retains terminal results until the operator exits. If the backend
fails to start, its log tail is printed before the temporary session directory
is removed. Requests and subscription setup have bounded timeouts; malformed or
incompatible protocol messages are shown as errors instead of crashing a socket
callback.

## Architecture

The Python backend owns the validated, append-only event contract and serves it
as JSONL over a private Unix socket. `src/generated/` is generated from those
Pydantic models. The TypeScript client owns framing and request correlation,
`session-controller.ts` owns effects, `session-model.ts` and `run-map.ts` reduce
events into presentation state, and `ui/` owns OpenTUI rendering and input.

Conversation state retains at most 1,000 semantic entries. Rendering is keyed
by entry identity: state-only updates reuse existing cards, streamed tail
updates replace only the final card, and a full rebuild is reserved for filter
or history-window changes. Typed tool calls use stable call IDs so parallel
results return to the correct card; old event logs without IDs use a documented
FIFO-by-tool fallback.

## Development

From the repository root:

```bash
pnpm install --frozen-lockfile
pnpm --dir clients/tui generate:protocol
pnpm --dir clients/tui check
pnpm --dir clients/tui test
pnpm --dir clients/tui build
pnpm check:ts
uv run pytest tests/test_tui.py tests/agents/test_callbacks.py tests/render/test_sink.py
```

After changing Python protocol models, regenerate both files in
`src/generated/` and review their diff. The test suite covers reducer behavior,
OpenTUI frames and navigation, launcher cleanup, socket fragmentation and
timeouts, replay/live delivery, and the Python supervision service.
