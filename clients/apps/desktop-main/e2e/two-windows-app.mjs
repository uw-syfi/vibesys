/**
 * Real-Electron composition for two concurrent desktop windows attached to two different runs.
 *
 * Fixture-driven, like `welcome-recovery-app.mjs`: production owns `AppWindow` and `RunControl`
 * unchanged. The host they talk to is a small local stand-in (`FixtureHostConnection` below), not
 * a real SSH host or `vibesys-helper` subprocess: it answers `RunControl`'s `HostConnection`
 * interface directly (`instances.list`, `openRun`), and each run's own channel answers the real
 * run protocol (`query.snapshot`, `query.iterations`, `subscribe`) with content unique to that run,
 * the same way the TUI's demo backend stands in before a real session hooks up.
 *
 * This is package-local by design, not layered on `@vibesys/connection`'s `ConnectionManager`/
 * `Link`/`FakeHelper`: this app's own architecture check forbids shipped code from importing
 * another package's test support, and an E2E entry under `apps/desktop-main/e2e` is shipped code
 * by that rule (it is scanned like the rest of the package). `RunControl` only needs a
 * `HostConnection`-shaped object (`instances.list`, `openRun`, `onPhase`/`watchInstances`/
 * `onRunUnreachable`), so this fixture implements just that, over a plain `node:stream` `Duplex`
 * for each run's channel. There is no genuine second host or SSH link here; this proves the
 * desktop shell's own multi-window wiring (`RunControl`'s per-`run_id` bindings, the
 * window-close-detach path, and `RunIpcRegistry`'s cross-window connect admission), not a live
 * backend or the connection manager's own reconnect/sharing behavior (covered by
 * `@vibesys/connection`'s own tests).
 */
// First, before any page loads: see the module for the Playwright race it closes.
import './pin-performance-global.mjs';
import {Duplex} from 'node:stream';
import {app, ipcMain, webContents} from 'electron';
import {RunControl} from '@vibesys/feature-run-control/main';
import {firstLoad} from './first-load.mjs';

// Resolve after `test:e2e` builds the package, exactly as `welcome-recovery-app.mjs` does: this
// entry sits outside the production graph, so Playwright drives it without a test-only API on the
// shipped app.
const [{confineContents, lockDownSession, registerAppScheme, serveBundledUi}, {AppWindow}] =
  await Promise.all([
    import(new URL('../dist/main/app-security.js', import.meta.url)),
    import(new URL('../dist/main/window.js', import.meta.url)),
  ]);

const RUN_URL = 'app://vibesys/desktop.html';
const HOST = {kind: 'local'};
// The welcome preload asks for what the first paint needs before the page's scripts run.
ipcMain.on('vibesys:welcome:first-paint', event => {
  event.returnValue = {theme: 'paper', chromeInset: {width: 84, height: 40}};
});

/** One simulated run: its identity, and the distinguishing content its fixture server answers. */
const RUNS = {
  a: {instanceId: '00000000000a', runId: 'run-alpha', sequence: 11_017, transcriptEntries: 3},
  b: {instanceId: '00000000000b', runId: 'run-bravo', sequence: 22_031, transcriptEntries: 1},
};

const logs = [];
const fatals = [];
/** `AppWindow` instances by label, built once `app.whenReady()` resolves. */
const windows = {};
/** Each window's current run page `webContents.id`, correlated when `openPage` creates it. */
const runPageContentsId = new Map();
/** Each run page's first load, by run id: settles on the page's own load events. */
const runPageLoads = new Map();

function log(message) {
  logs.push(message);
}

/** A minimal, valid `LiveInstanceRecord` for `instanceId`/`runId` (the fields `attach` reads). */
function liveRecord(instanceId, runId, project) {
  return {
    version: 4,
    id: instanceId,
    status: 'serving',
    phase: {status: 'running', stage: null, reason: null},
    socket_path: `/tmp/${instanceId}.sock`,
    log_path: `/tmp/${instanceId}.log`,
    project_root: project,
    run_id: runId,
    pid: 4242,
    started_at: 1,
    ended_at: null,
    hostname: 'fixture',
    protocol_version: 1,
    vibesys_version: '0.1.1',
    vibesys_root: '/src/vibesys',
  };
}

/** A fixture "server" for one run: answers the run protocol with content unique to this run. */
function runServerFor({runId, sequence, transcriptEntries}) {
  return stream => {
    stream.onLine(line => {
      let request;
      try {
        request = JSON.parse(line);
      } catch {
        return;
      }
      const base = {
        protocol_version: 1,
        request_id: request.request_id,
        timestamp: '2026-01-01T00:00:00Z',
        ok: true,
        error: null,
      };
      if (request.type === 'query.snapshot') {
        stream.write(JSON.stringify({...base, snapshot: {run_id: runId, sequence, status: 'running'}}));
        return;
      }
      if (request.type === 'query.iterations') {
        stream.write(JSON.stringify({...base, iterations_ready: false}));
        return;
      }
      if (request.type === 'subscribe') {
        stream.write(
          JSON.stringify({
            type: 'subscribed',
            request_id: request.request_id,
            run_id: runId,
            latest_sequence: sequence,
          }),
        );
        const events = Array.from({length: transcriptEntries}, (_unused, index) => ({
          timestamp: '2026-01-01T00:00:00Z',
          type: 'agent_output_chunk',
          sequence: sequence + index + 1,
          data: {kind: 'agent_output_chunk', channel: 'assistant', content: `${runId}-chunk-${index}`},
        }));
        stream.write(
          JSON.stringify({
            type: 'event_batch',
            events,
            through_sequence: sequence + transcriptEntries,
            store_id: `store-${runId}`,
            history_after_sequence: 0,
          }),
        );
        return;
      }
      // Any other query (chat, tui_defaults, ...): answer minimally rather than leave the
      // renderer's request pending forever.
      stream.write(JSON.stringify(base));
    });
  };
}

/** Wrap a `stream => void` run server (newline-JSON `onLine`/`write`) as a real `Duplex`. */
function duplexRunChannel(serveLines) {
  let buffer = '';
  let onLine = () => {};
  const duplex = new Duplex({
    read() {},
    write(chunk, _encoding, callback) {
      buffer += chunk.toString('utf8');
      let newline = buffer.indexOf('\n');
      while (newline !== -1) {
        const line = buffer.slice(0, newline);
        buffer = buffer.slice(newline + 1);
        if (line.length > 0) onLine(line);
        newline = buffer.indexOf('\n');
      }
      callback();
    },
  });
  serveLines({
    onLine: listener => {
      onLine = listener;
    },
    write: line => duplex.push(`${line}\n`),
  });
  return duplex;
}

/**
 * `RunControl`'s `HostConnection` dependency, answering `instances.list` from a fixed live listing
 * and opening each run's channel over a `duplexRunChannel`. One instance is shared by both windows
 * (the same `HOST`), matching production: `ConnectionManager` shares one connection per host too.
 */
class FixtureHostConnection {
  phase = {kind: 'ready'};
  /** Each run channel's dial signal, by instance id, so the control surface can read it later. */
  openSignals = new Map();

  #listing;
  #servers;

  constructor(records, servers) {
    this.#listing = {version: 4, instances: records, ended: [], unreadable: [], unverified: []};
    this.#servers = servers;
  }

  async request(req) {
    if (req.type === 'instances.list') return {listing: this.#listing};
    throw new Error(`fixture host connection: unsupported request ${req.type}`);
  }

  onPhase(_listener) {
    return () => {};
  }

  watchInstances(listener) {
    listener(this.#listing);
    return () => {};
  }

  onRunUnreachable(_listener) {
    return () => {};
  }

  dispatch(_action) {}

  async openRun(instanceId, signal) {
    this.openSignals.set(instanceId, signal);
    const serveLines = this.#servers.get(instanceId);
    if (serveLines === undefined) throw new Error(`no fixture run server for ${instanceId}`);
    return {stream: duplexRunChannel(serveLines)};
  }
}

function requiredContents(contents, label) {
  if (contents === null || contents.isDestroyed()) {
    throw new Error(`the ${label} contents are unavailable`);
  }
  return contents;
}

registerAppScheme();
app.on('web-contents-created', (_event, contents) => confineContents(contents, log));
app.on('window-all-closed', () => app.quit());

// See `welcome-recovery-app.mjs`: schedule work after this module finishes evaluating, so
// Playwright's substituted `app.whenReady()` is not awaited at top level.
void app.whenReady().then(() => {
  lockDownSession(log);
  serveBundledUi();

  const connection = new FixtureHostConnection(
    [
      liveRecord(RUNS.a.instanceId, RUNS.a.runId, '/projects/alpha'),
      liveRecord(RUNS.b.instanceId, RUNS.b.runId, '/projects/bravo'),
    ],
    new Map([
      [RUNS.a.instanceId, runServerFor(RUNS.a)],
      [RUNS.b.instanceId, runServerFor(RUNS.b)],
    ]),
  );

  /** Which window's `openRunPage` an attach's eventual `run_id` opens in, chosen before attaching. */
  const openPageTargets = new Map([
    [RUNS.a.runId, 'a'],
    [RUNS.b.runId, 'b'],
  ]);

  const runs = new RunControl({
    connection: async () => ({connection, [Symbol.asyncDispose]: async () => {}}),
    openPage: (runId, title) => {
      const label = openPageTargets.get(runId);
      if (label === undefined) throw new Error(`no window is assigned to run ${runId}`);
      const target = windows[label];
      const before = new Set(webContents.getAllWebContents().map(contents => contents.id));
      const page = target.openRunPage(runId, title);
      const created = webContents.getAllWebContents().find(contents => !before.has(contents.id));
      if (created !== undefined) {
        runPageContentsId.set(runId, created.id);
        runPageLoads.set(runId, firstLoad(created));
      }
      return page;
    },
    confirm: async () => true,
    remember: async () => {},
    changed: () => {},
    log,
    now: () => Date.now(),
  });

  for (const label of ['a', 'b']) {
    windows[label] = new AppWindow({
      log,
      surfaceUrl: 'app://vibesys/welcome.html',
      connect: (port, runId) => runs.connect(port, runId),
      closed: runId => runs.detach(runId),
      fatal: error => fatals.push(error.message),
    });
  }

  runsRef = runs;
  connectionRef = connection;
});

globalThis.vibesysTwoWindowsE2E = {
  async attach(label) {
    const target = RUNS[label];
    if (target === undefined) throw new Error(`no such run: ${label}`);
    await runs_().attach(HOST, target.instanceId);
  },
  windowState(label) {
    const window = windows[label];
    if (window === undefined) return null;
    return {mode: window.mode};
  },
  async inspectRun(label) {
    const window = windows[label];
    if (window === undefined) throw new Error(`no such window: ${label}`);
    const id = runPageContentsId.get(windowRunId(label));
    const contents = id === undefined ? null : (webContents.fromId(id) ?? null);
    return requiredContents(contents, `${label}'s run`).executeJavaScript(
      '(() => ({bodyText: document.body.textContent ?? \'\', title: document.title}))()',
      true,
    );
  },
  runWebContentsIds() {
    return webContents
      .getAllWebContents()
      .filter(contents => contents.getURL() === RUN_URL && !contents.isDestroyed())
      .map(contents => contents.id);
  },
  /** Resolves once the run page `attach(label)` opened has loaded; rejects if it never will. */
  async runPageLoaded(label) {
    const load = runPageLoads.get(windowRunId(label));
    if (load === undefined) throw new Error(`window ${label} has opened no run page`);
    await load;
  },
  /** The run page `webContents.id` `attach(label)` opened, named rather than inferred by order. */
  contentsIdFor(label) {
    return runPageContentsId.get(windowRunId(label)) ?? null;
  },
  setMode(label, mode) {
    const window = windows[label];
    if (window === undefined) throw new Error(`no such window: ${label}`);
    window.setMode(mode);
  },
  closeWindow(label) {
    const window = windows[label];
    if (window === undefined) throw new Error(`no such window: ${label}`);
    window.close();
  },
  hasContents(id) {
    return webContents.fromId(id) !== undefined;
  },
  state() {
    // Exposed before `app.whenReady()` finishes so Playwright can begin polling immediately
    // (see the comment above `app.whenReady().then(...)`); `ready` lets the spec wait out that
    // startup race on a control method that never throws, instead of polling `attach` itself and
    // turning an ordinary startup race into a terminal evaluate error.
    return {ready: runsRef !== null && connectionRef !== null, logs: [...logs], fatals: [...fatals]};
  },
  /**
   * Whether each run's own channel (the dial `relay()` opened through `openRun`) has been told to
   * end. A window's close must abort only its own run's channel signal, never the other window's:
   * `run-control.ts`'s old single `#run` slot would have let one window's close reach the other's.
   */
  channelStates() {
    const states = {};
    for (const [instanceId, signal] of connectionRef.openSignals) {
      states[instanceId] = {aborted: signal.aborted};
    }
    return states;
  },
};

let runsRef = null;
function runs_() {
  if (runsRef !== null) return runsRef;
  throw new Error('run control is not ready yet');
}

let connectionRef = null;

function windowRunId(label) {
  return RUNS[label]?.runId;
}
