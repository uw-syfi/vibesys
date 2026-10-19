# Desktop app development


The connection design (one host helper, one connection manager, explicit run identity and
lifecycle phases, per-screen stores) is in [desktop-connection.md](desktop-connection.md); this
section says how the code is laid out and how to run it.

The desktop app (`clients/apps/desktop-main`) ships its own UI (`clients/apps/desktop-shell`) and
reaches servers through its main process, without the web gateway. A server is
a detached run (`vibesys --detach`) on this machine
or on an SSH host. The main process reaches every host the same way: it runs the
host helper there (`python -m entrypoints.host_helper`) and speaks its typed
protocol; This Mac and an SSH host differ only in how the helper is started, and
the page cannot tell them apart.

```bash
# The main window: the sidebar, New run, Machines, Settings
pnpm --dir clients --filter @vibesys/app-desktop-main start

# One of a host's detached runs, in a window of its own (--instance alone: one on this machine)
pnpm --dir clients --filter @vibesys/app-desktop-main start -- --host HOST --instance ID

# Start a detached run for a local project and open it in a window of its own
pnpm --dir clients --filter @vibesys/app-desktop-main start -- --project ~/proj [-- RUN_ARGS...]
```

`start` builds the app, including the page bundle the windows load from the `app://vibesys` origin
(`@vibesys/app-desktop-shell`'s `build`, from `clients/apps/desktop-shell/shell.html`). The page has
no network access: the main process cancels every http(s) and ws(s) request and the page's Content
Security Policy forbids connections. Its only way to the server is the preload bridge
(`window.vibesysWelcome`, whose `connectRun` takes no host or path): the main process binds a window
to one run.

### Test a branch in the app

```bash
./support/repoctl/repoctl dev                          # build this checkout, open the app
./support/repoctl/repoctl dev --machine host-a         # also ship HEAD to host-a first
./support/repoctl/repoctl dev --machine host-a --dry-run
```

`dev` installs the client packages only when the lockfile or a package manifest changed since the
last successful install (a stamp under `build/dev/`), builds the app and the packages it uses with
the build `test:e2e` runs, syncs this checkout's Python environment, and opens the app on a profile
of its own, `build/dev/desktop-profile` (`VIBESYS_USER_DATA_DIR`), so the installed app and other
Electron apps keep their settings. It returns when the app closes. Commands and paths are in
`.repoctl/dev.toml`.

With `--machine`, the committed HEAD is pushed over ssh to a dev ref in the machine's checkout (no
GitHub involved) and checked out detached; a checkout that is not a git working tree gets its
tracked files by rsync. The machine's checkout is the one the desktop app saved for it (its `hosts.json`, set on the
Machines screen); `--checkout` overrides it, and an unsaved machine is an error naming the file. `dev` refuses before changing anything if the local tree or the
machine's checkout has uncommitted changes to tracked files. It then runs `uv sync` there and starts
a fresh helper the way the app does, and fails unless that helper's `hello` reports the shipped
commit within 60 seconds. Each step prints its duration. The helper keeps no state and every run is
a detached server, so starting a helper never interrupts a live run; there is no `--force`.
Shipping does replace the checkout's files under a live run, so ship when no run is in a phase that
imports new code.

### Layout

The Electron main process is composed from packages, as the renderer is; the package-graph
checker holds the boundaries between them, and `clients/.dependency-cruiser.mjs` the modules
inside them:

- `clients/apps/desktop-main` (`@vibesys/app-desktop-main`) is composition only: `src/main/` is
  the composition root, the windows, the lifecycle, page confinement, launch arguments, and the
  shell's IPC boundary, which routes each request `@vibesys/ipc` parsed to the feature that owns
  it. `src/preload/` holds the one sandboxed preload (`welcome.cts`, the surface's historical
  name), a single file, which the main process loads by path and never imports.
- The features' `./main` entries are their main-process controllers: `@vibesys/feature-hosts`
  (`Hosts`: checkouts, tasks, path completion, pushed listings) and
  `@vibesys/feature-run-control` (`RunControl`: attach, start, resume, stop, and the relay of a
  window's run connections). These packages have no renderer; the screens are the strict features
  (`app-shell`, `settings`, `machines`, `new-run`, `run-page`).
- `@vibesys/connection` is the connection manager, a pure `state/` machine and a `shell/`; only
  its shell starts processes or opens sockets. `@vibesys/settings` is `hosts.json` and
  `recent.json` with `SettingsStore` as their one writer.
- The renderer is `clients/apps/desktop-shell` (`@vibesys/app-desktop-shell`), composition only:
  `main.tsx` (the entry) and `shell.tsx` (each strict feature's store over the bridge, and the
  screen the sidebar selects). The screens are built from `@vibesys/ui` alone.
- The processes share only workspace packages: `@vibesys/hosts` (host naming, a host's listed
  runs), `@vibesys/runs` (ids, recent runs, the stop and resume decisions), and `@vibesys/ipc`
  (the run connection's port messages and the typed shell IPC, each feature's requests with the
  parser the main process runs on them).

`apps/desktop-main` declares one entry per process: `.` (`node`, `src/main/index.ts`, the Electron
main) and `./preload-welcome` (`preload`, the preload file), so the package-graph checker holds
each side to its runtime and its third-party imports, and a preload to loading only Electron.
`src/preload/welcome.test.ts` drives the preload's bridge into
`@vibesys/ipc`'s parser, so the renderer and the main process agree on every channel's arguments.

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

### The desktop shell

The window opens on the app shell (`clients/apps/desktop-shell/shell.html`, a React bundle with
the preload above): the sidebar (live and recent runs, Machines), New run, Machines, Settings, and
the Run screen. The main process answers its requests only for a window's own shell document and
validates every value before it reaches a host. Host data is pushed, never polled: the main
process watches each shown host's registry (`instances.watch`) and sends every change
(`HostListing`); recent runs' statuses derive from those listings. The checkout and project fields
complete paths as you type (`paths.complete`: one listing per directory, filtered in the view).

On first use of an SSH host the Machines screen asks for the checkout on that host and checks the
answer by starting the helper there before saving it (`hosts.json`, version 2: one `checkout` per
host key); This Mac defaults to the checkout the app was built from. New run lists a project's
tasks (`tasks.list`) and starts it (`run.start`). Recent rows are stored by `(host, run id)` from
the run's record when the app starts or attaches to it (`recent.json`, version 2).

A shown run is attached to its window, and the Run screen's session is that window's. Each
connection is a MessagePort the main process relays to a new run channel
(`features/run-control/relay.ts`), reframing one port message as one newline-delimited line in each
direction. The session (`@vibesys/run-session`) keeps its resume and reconciliation logic but no
retry timer of its own: the main process wakes the page when the host connection recovers.

The screens' stores are pure reducers or XState machines (`clients-architecture.md`, "State"); each
feature's `renderer/ui/` is presentational and `renderer/effects/` its shell, and the app's
`main.tsx` is the composition root. They are tested model-based with `checkModel` from
`@vibesys/store/testing`.

`AppLifecycle` (`main/app/app-lifecycle.ts`) is the one owner of process-wide state: a
single-instance lock (a second launch focuses the first window and opens what it was asked to)
and a pure quit machine (release every connection once, bounded, then exit through the one
`app.exit` call site).
