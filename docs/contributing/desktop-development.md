# Desktop app development


The connection design (one host helper, one connection manager, explicit run identity and
lifecycle phases) is in [desktop-connection.md](desktop-connection.md); this
section says how the code is laid out and how to run it.

The desktop app (`clients/apps/desktop`, `@vibesys/desktop`) is an Electron app that operates
orchestrated runs: the operator picks a host, starts a run, watches its agent map, messages an
agent and stops the run. It reaches servers through its main process, without the web gateway. A
host is this machine or an SSH host; the main process reaches every host the same way, by running
the host helper there (`python -m entrypoints.host_helper`) and speaking its typed protocol, so
the page cannot tell them apart. The page talks to the orchestrator daemon on the host through the
`vibesys-gateway` binary that `build` compiles (it needs Go).

```bash
# Install once, then build and open the app (the main window lists the saved hosts)
pnpm --dir clients install
pnpm --dir clients --filter @vibesys/desktop start

# Open it on named hosts (a saved host's key, as in hosts.json)
pnpm --dir clients --filter @vibesys/desktop start -- --host=ssh:host-a
```

`start` builds the app (`tsc -b` for each folder, `vite build` for the page in `dist/renderer`, and
the gateway binaries in `dist/gateway`) and runs `electron .`. The page has no network access: the
main process cancels every http(s) and ws(s) request, denies permissions, blocks navigation and
new windows, and the page's Content Security Policy forbids connections. Its only way to a host is
the preload's `window.vibesysGateway`, which hands the page one `MessagePort` per call; the main
process admits a port only for its own window. `VIBESYS_DESKTOP_USER_DATA` points the app's
settings and Chromium data at another directory.

`pnpm --filter @vibesys/desktop build:fixture` builds the same screens over a scripted fake daemon
(`dist/fixture`, served by any static server, with `?scenario=<seed>-<subagents>-<live|done>`). It
needs no Electron, host or daemon, and is how a screen is looked at without a display.

### Test a branch in the app

```bash
./support/repoctl/repoctl dev                          # build this checkout, open the app
./support/repoctl/repoctl dev --machine host-a         # also ship HEAD to host-a first
./support/repoctl/repoctl dev --machine host-a --dry-run
```

`dev` installs the client packages only when the lockfile or a package manifest changed since the
last successful install (a stamp under `build/dev/`), builds the app and the packages it uses
(`turbo run build`), syncs this checkout's Python environment, and opens the app on a profile
of its own, `build/dev/desktop-profile` (`VIBESYS_DESKTOP_USER_DATA`), so the installed app and other
Electron apps keep their settings. It returns when the app closes. Commands and paths are in
`.repoctl/dev.toml`.

With `--machine`, the committed HEAD is pushed over ssh to a dev ref in the machine's checkout (no
GitHub involved) and checked out detached; a checkout that is not a git working tree gets its
tracked files by rsync. The machine's checkout is the one the desktop app saved for it (its `hosts.json`); `--checkout` overrides it, and an unsaved machine is an error naming the file. `dev` refuses before changing anything if the local tree or the
machine's checkout has uncommitted changes to tracked files. It then runs `uv sync` there and starts
a fresh helper the way the app does, and fails unless that helper's `hello` reports the shipped
commit within 60 seconds. Each step prints its duration. The helper keeps no state and every run is
a detached server, so starting a helper never interrupts a live run; there is no `--force`.
Shipping does replace the checkout's files under a live run, so ship when no run is in a phase that
imports new code.

### Layout

One package, a folder per role (`clients-architecture.md`, "The Effect desktop app"); the
package-graph checker holds the boundaries between packages, and `clients/.dependency-cruiser.mjs`
the folders inside the app:

- `src/main/` is the Electron composition root: the connection manager and settings store, the
  gateway IPC (`gateway.ts`, `port-ipc.ts`), page confinement and the window (`window-policy.ts`).
- `src/preload/` is the one sandboxed preload; it loads only Electron.
- `src/core/` is the Effect client: the typed transport, the orchestrator client and the
  per-host session (connection, run and agent feeds, commands) as services and atoms.
- `src/state/` is the plain folds and views (the run model, routes, feed status); it imports no
  Effect.
- `src/ui/` is the React screens and the renderer entry; `src/testing/` holds the Fakes and the
  fixture page, which only tests import.
- `src/main/` is the main process shell: Effect services over a process seam and a socket seam
  (`process.ts`, `local-host.ts`), the ssh link, the gateway dialers and the saved-hosts file. Only
  those two files start processes or open sockets.

### Connections

The main process is the only code that creates links and channels (`src/main/`; the services and
their order are described under "The main process shell" in
[clients-architecture.md](clients-architecture.md#state)):

- `SshLink` reaches one host with the system `ssh`, so `~/.ssh/config`, ProxyJump, agents and
  certificates work unchanged. The app owns one master connection per host (`ControlMaster`,
  `ControlPersist`, `ServerAliveInterval`), shared by every channel to that host. Every channel is a
  `BatchMode=yes` exec session, so the app never prompts: a host that wants a password or an unknown
  host key fails as `auth` with ssh's own words, and the user fixes it in their terminal.
- `GatewayDialers` start `vibesysd connect` on the host (this machine, or over the ssh link, uploading
  the gateway binary when the host lacks the right one). The gateway is the only program on the host
  the app talks to; it starts no Python.
- `GatewayConnection` is one host's HTTP/2 gRPC session: a `Hello` that compares the API schema hash,
  then calls and streams. One reconnect policy lives in `state/gateway-policy.ts`; a call in flight
  when the link drops fails with `UNAVAILABLE` and is never re-issued, so a command cannot run twice.
- `GatewayCalls` serves the window's calls: each call is one `MessagePort`, framed by
  `core/gateway-codec.ts`, validated against the generated bindings on both sides.

Every service has a Fake in `src/testing/main/` (a process, a gateway, a local host), so the shell is
tested with no real ssh, socket or child.

### The window

The window opens on the page `src/ui/main.tsx` renders: a session per host (`makeHostSession`) and
the orchestrator screens over it. The runs list shows each host's runs and whether the host is
connected; Start run takes a repository, a task, a fanout limit, the lead agent's harness and an
optional brief; a run opens its agent map (the lead and its subagents, each with its harness, live
status, timeline and handoffs), an agent opens a panel to message it, and Stop run ends the run.
Every command shows what became of it (sending, accepted, refused with the reason, or unconfirmed
with a Try again). A connection that is not working says so above what it makes out of date, with
a Retry.

Host data is pushed, never polled: each feed is a stream the session restarts after a reconnect
and folds into state; a view reads atoms over that state.

`main/index.ts` releases the gateway connections once, bounded, when the app quits.
