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
- `@vibesys/connection` is the connection manager, a pure `state/` machine and a `shell/`; only
  its shell starts processes or opens sockets. `@vibesys/settings` is `hosts.json` and
  `recent.json` with `SettingsStore` as their one writer.

### Connections

`@vibesys/connection` is the only code that creates links and channels
(`docs/contributing/desktop-connection.md`, "Connection manager"):

- A `Link` reaches one host. `LocalLink` starts each helper as a child of `/bin/sh`; `SshLink`
  uses the system `ssh`, so `~/.ssh/config`, ProxyJump, agents, and certificates work unchanged.
  The app owns one master connection per host (`ControlMaster`, `ControlPath=/tmp/vsd-<uid>/%C`,
  `ControlPersist=30m`, `ServerAliveInterval`), shared by every connection to that host. Only the
  master authenticates, through an askpass helper that shows ssh's prompt in a native dialog, and
  only when the user asks (a machine's sign-in); a host key is never accepted
  silently. Every helper is a `BatchMode` exec channel over the master.
- Both links start the helper with the same bootstrap (`shell/bootstrap.ts`), the only shell text
  the app sends to a host: `sh -c '<one-line script>' vibesys-helper xHEX`, a fixed script with no
  quote or line break, its one datum (the checkout) hex-encoded for `sh` to decode, so it means the
  same under any login shell. It checks the checkout, finds `uv` (the non-interactive PATH, a
  login shell's PATH, or a few common directories), and `exec`s `uv run --no-sync --project
  CHECKOUT python -I -m entrypoints.host_helper`; otherwise it exits 64 (the checkout predates the
  helper), 65 (not a VibeSys checkout), or 66 (no `uv`), with a word saying which.
- `HelperChannel` is one channel: `hello`, then requests answered concurrently by id, each under
  a deadline, every line parsed strictly against the generated contract
  (`@vibesys/contracts/host-helper`). A run channel's one `run.connect` makes it a byte relay
  carrying the run protocol; its end is the helper's exit status and `ChannelEnd` line.
- `HostConnection` is one host's connection: a pure machine (`state/host-connection.ts`: idle,
  linking, handshaking, ready, reconnecting, offline, auth-needed, incompatible, closed) under a
  shell that owns the control channel, every run channel, and every timer through one
  `AsyncDisposableStack` and `AbortSignal`. It is the app's one retry loop per host, and its check
  is the failing operation: the control channel is checked by `ping`, a run channel by a fresh
  `run.connect` to the same run. Callers never retry: a request or a run channel waits until the
  host is ready, or fails with the machine's terminal reason.
- `ConnectionManager` keeps one `HostConnection` per host, and owns every connection and link:
  quitting closes every channel and master once. Runs are detached: closing the app never ends a
  run (`vibesys instances stop ID` does).

Every implementation passes one contract suite (`testing/host-connection-contract.ts`): the
in-memory `FakeHelper`, which speaks the helper protocol on the wire and replays the `helper-*`
conformance scenarios, and the real helper of this checkout (`VIBESYS_REAL_CONTRACTS=1`).

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

`main/index.ts` releases the connection manager once, bounded, when the app quits.
