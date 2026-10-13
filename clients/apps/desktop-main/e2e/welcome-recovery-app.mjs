/**
 * Real-Electron composition for the welcome-recovery journey. Production owns both views,
 * preloads, IPC binders, renderer confinement, and lifecycle wiring. In-memory implementations
 * replace only host/run/settings I/O so the journey needs no subprocess, socket, or user files.
 */
import {app, BaseWindow, webContents} from 'electron';

// Resolve after `test:e2e` builds the package. Keeping this entry outside the production graph
// gives Playwright control without adding a test-only API to the shipped application.
const [
  {confineContents, lockDownSession, registerAppScheme, sandboxedView, serveBundledUi},
  {Desktop},
  {WelcomeFeed},
  {AppWindow},
] = await Promise.all([
  import(new URL('../dist/main/app-security.js', import.meta.url)),
  import(new URL('../dist/main/desktop.js', import.meta.url)),
  import(new URL('../dist/main/welcome-feed.js', import.meta.url)),
  import(new URL('../dist/main/window.js', import.meta.url)),
]);

const WELCOME_URL = 'app://vibesys/welcome.html';
const RUN_URL = 'app://vibesys/desktop.html';
const CREDIT_BYTES = 16 * 1024 * 1024;
const INSTANCE_ID = '0123456789ab';
const logs = [];
const fatals = [];
let appWindow = null;
let unauthorizedWindow = null;
let unauthorizedView = null;

const attached = {
  host: 'local',
  hostLabel: 'This Mac',
  instanceId: INSTANCE_ID,
  runId: 'run-e2e',
  project: '/projects/e2e',
  connection: {kind: 'connected'},
};

function log(message) {
  logs.push(message);
}

function frameBytes(data) {
  return Buffer.byteLength(data, 'utf8') + 1;
}

/** Deterministic protocol peer for the production run preload's real MessagePort. */
class ProtocolPeer {
  #port;
  #receiveCredit = 0;
  #closed = false;

  constructor(port) {
    this.#port = port;
    port.onMessage(value => this.#receive(value));
    port.onClose(() => {
      this.#closed = true;
    });
    port.post({type: 'open', creditBytes: CREDIT_BYTES});
  }

  #receive(value) {
    if (this.#closed || typeof value !== 'object' || value === null) return;
    if (value.type === 'close') {
      this.#closed = true;
      this.#port.close();
      return;
    }
    if (value.type === 'credit') {
      this.#receiveCredit += value.bytes;
      return;
    }
    if (value.type !== 'send' || typeof value.data !== 'string') return;
    this.#port.post({type: 'credit', bytes: frameBytes(value.data)});
    this.#answer(JSON.parse(value.data));
  }

  #post(message) {
    const data = JSON.stringify(message);
    const bytes = frameBytes(data);
    if (this.#closed || bytes > this.#receiveCredit) {
      throw new Error('the run page did not grant enough protocol credit');
    }
    this.#receiveCredit -= bytes;
    this.#port.post({type: 'message', data, bytes});
  }

  #answer(request) {
    if (request.type === 'query.snapshot') {
      this.#post({
        protocol_version: 1,
        request_id: request.request_id,
        timestamp: '2026-01-01T00:00:00Z',
        ok: true,
        snapshot: {run_id: 'run-e2e', sequence: 0, status: 'running'},
      });
      return;
    }
    if (request.type === 'query.iterations') {
      this.#post({
        protocol_version: 1,
        request_id: request.request_id,
        timestamp: '2026-01-01T00:00:00Z',
        ok: true,
        iterations_ready: false,
      });
      return;
    }
    if (request.type === 'subscribe') {
      this.#post({
        type: 'subscribed',
        request_id: request.request_id,
        run_id: 'run-e2e',
        latest_sequence: 0,
      });
      this.#post({
        type: 'event_batch',
        events: [],
        store_id: 'store-e2e',
        through_sequence: 0,
        history_after_sequence: 0,
      });
    }
  }
}

class FakeRuns {
  attached() {
    return attached;
  }

  stops() {
    return {};
  }

  connect(port) {
    new ProtocolPeer(port);
  }

  retry() {}
  detach() {}
}

class FakeHosts {
  settingsProblem() {
    return Promise.resolve(null);
  }

  aliases() {
    return Promise.resolve([]);
  }

  host(id) {
    return Promise.resolve({
      key: id.kind === 'local' ? 'local' : `ssh:${id.alias}`,
      label: id.kind === 'local' ? 'This Mac' : id.alias,
      checkout: '/src/vibesys',
    });
  }

  recent() {
    return Promise.resolve();
  }

  retireInspectionOwner() {}
  cancelInspectionOwner() {}
  cancelInspection() {}
  cancelCheckoutOwner() {
    return Promise.resolve();
  }
  retireOwner() {
    return Promise.resolve();
  }
}

class FakeSettings {
  onRecentChanged() {}
  recent() {
    return Promise.resolve({version: 3, runs: []});
  }
}

function contentsAt(url) {
  return webContents.getAllWebContents().find(contents => contents.getURL() === url) ?? null;
}

function ownedWelcomeId() {
  if (appWindow === null) throw new Error('the app window is unavailable');
  const owner = appWindow.welcome;
  if (typeof owner.currentId === 'function') return owner.currentId();
  // Merge-base compatibility: before #2270, AppWindow exposed the one concrete view directly.
  return owner.webContents.id;
}

function ownedWelcomeContents() {
  const id = ownedWelcomeId();
  return id === null ? null : (webContents.fromId(id) ?? null);
}

function requiredContents(contents, label) {
  if (contents === null || contents.isDestroyed()) {
    throw new Error(`the ${label} contents are unavailable`);
  }
  return contents;
}

function inspectWelcomeContents(contents) {
  return requiredContents(contents, 'welcome').executeJavaScript(
    `(() => {
      const visible = element => {
        if (element === null) return false;
        const style = getComputedStyle(element);
        const bounds = element.getBoundingClientRect();
        return style.display !== 'none' && style.visibility !== 'hidden' && bounds.width > 0 && bounds.height > 0;
      };
      const text = selector => Array.from(document.querySelectorAll(selector), element => element.textContent?.trim() ?? '');
      return {
        titleStripVisible: visible(document.querySelector('#title-strip')),
        headings: text('h1, h2, h3'),
        buttons: text('button'),
        bodyText: document.body.textContent ?? '',
        confinement: {
          bridge: typeof window.vibesysWelcome,
          nodeRequire: typeof globalThis.require,
          nodeProcess: typeof globalThis.process,
        },
      };
    })()`,
    true,
  );
}

function welcomeHosts(contents) {
  return requiredContents(contents, 'welcome').executeJavaScript(
    'window.vibesysWelcome.hosts()',
    true,
  );
}

function clickWelcomeButton(label) {
  return requiredContents(ownedWelcomeContents(), 'welcome').executeJavaScript(
    `(() => {
      const label = ${JSON.stringify(label)};
      const button = Array.from(document.querySelectorAll('button')).find(
        element => element.textContent?.trim() === label,
      );
      if (button === undefined) throw new Error('welcome button is unavailable: ' + label);
      button.click();
    })()`,
    true,
  );
}

registerAppScheme();
app.on('web-contents-created', (_event, contents) => confineContents(contents, log));
app.on('window-all-closed', () => app.quit());

// Playwright's Electron loader releases its substituted `app.whenReady()` only after attaching
// both debuggers. Awaiting that promise at module top level prevents the loader from completing
// the attachment and deadlocks `electron.launch()`. Schedule readiness work so this entry module
// finishes evaluating first; the test polls the control below until initialization is complete.
void app.whenReady().then(() => {
  lockDownSession(log);
  serveBundledUi();

  const runs = new FakeRuns();
  const welcome = new WelcomeFeed();
  appWindow = new AppWindow({
    log,
    connect: port => runs.connect(port),
    closed: () => runs.detach(),
    fatal: error => fatals.push(error.message),
  });
  const desktop = new Desktop({
    window: appWindow,
    welcome,
    hosts: new FakeHosts(),
    runs,
    settings: new FakeSettings(),
    log,
  });
  appWindow.openRunPage('This Mac');
  desktop.chromeChanged();
});

globalThis.vibesysWelcomeRecoveryE2E = {
  state() {
    // The control surface is installed before `app.whenReady()` finishes so Playwright can begin
    // polling immediately. Initialization may still be constructing AppWindow at that instant;
    // expose a total not-ready snapshot rather than turning an ordinary startup race into a
    // terminal evaluate error.
    if (appWindow === null) {
      return {
        currentWelcomeId: null,
        currentWelcomeUrl: null,
        runId: null,
        runUrl: null,
        mode: null,
        logs: [...logs],
        fatals: [...fatals],
      };
    }
    const currentWelcomeId = ownedWelcomeId();
    const run = contentsAt(RUN_URL);
    return {
      currentWelcomeId,
      currentWelcomeUrl: ownedWelcomeContents()?.getURL() ?? null,
      runId: run?.id ?? null,
      runUrl: run?.getURL() ?? null,
      mode: appWindow.mode,
      logs: [...logs],
      fatals: [...fatals],
    };
  },
  crashWelcome() {
    const contents = ownedWelcomeContents();
    if (contents === null) throw new Error('the current welcome contents are unavailable');
    const id = contents.id;
    contents.forcefullyCrashRenderer();
    return id;
  },
  hasContents(id) {
    return webContents.fromId(id) !== undefined;
  },
  inspectWelcome() {
    return inspectWelcomeContents(ownedWelcomeContents());
  },
  welcomeHosts() {
    return welcomeHosts(ownedWelcomeContents());
  },
  clickWelcomeButton(label) {
    return clickWelcomeButton(label);
  },
  async openUnauthorizedWelcome() {
    unauthorizedWindow?.close();
    const holder = new BaseWindow({show: false, width: 960, height: 600});
    const view = sandboxedView('welcome.cjs');
    holder.contentView.addChildView(view);
    view.setBounds({x: 0, y: 0, width: 960, height: 600});
    unauthorizedWindow = holder;
    unauthorizedView = view;
    await view.webContents.loadURL(WELCOME_URL);
    return {unauthorizedId: view.webContents.id, currentId: ownedWelcomeId()};
  },
  inspectUnauthorizedWelcome() {
    const contents = unauthorizedView?.webContents ?? null;
    return inspectWelcomeContents(contents);
  },
  closeUnauthorizedWelcome() {
    unauthorizedWindow?.close();
    unauthorizedWindow = null;
    unauthorizedView = null;
  },
};
