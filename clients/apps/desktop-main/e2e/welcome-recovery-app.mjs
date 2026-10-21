/**
 * Real-Electron composition for the shell-recovery journey. Production owns the app shell page,
 * its preload, the IPC binders, renderer confinement, and lifecycle wiring. In-memory
 * implementations replace only host/run/settings I/O so the journey needs no subprocess, socket,
 * or user files.
 */
// First, before any page loads: see the module for the Playwright race it closes.
import './pin-performance-global.mjs';
import {app, BaseWindow, ipcMain, webContents} from 'electron';
import {firstLoad} from './first-load.mjs';

// Resolve after `test:e2e` builds the package. Keeping this entry outside the production graph
// gives Playwright control without adding a test-only API to the shipped application.
const [
  {confineContents, lockDownSession, registerAppScheme, sandboxedView, serveBundledUi},
  {Desktop},
  {WelcomeFeed},
  {rendererGeneration},
  {AppWindow},
  {chromeInset},
] = await Promise.all([
  import(new URL('../dist/main/app-security.js', import.meta.url)),
  import(new URL('../dist/main/desktop.js', import.meta.url)),
  import(new URL('../dist/main/welcome-feed.js', import.meta.url)),
  import(new URL('../dist/main/welcome-ipc.js', import.meta.url)),
  import(new URL('../dist/main/window.js', import.meta.url)),
  import(new URL('../dist/main/window-chrome.js', import.meta.url)),
]);

const SHELL_URL = 'app://vibesys/shell.html';
// The welcome preload asks for what the first paint needs before the page's scripts run.
ipcMain.on('vibesys:welcome:first-paint', event => {
  event.returnValue = {theme: 'paper', chromeInset: chromeInset(process.platform)};
});
const logs = [];
const fatals = [];
let appWindow = null;
let unauthorizedWindow = null;
let unauthorizedView = null;

function log(message) {
  logs.push(message);
}

class FakeRuns {
  connect(port, _runId) {
    port.close();
  }

  detach(_runId) {}
}

class FakeHosts {
  recent() {
    return Promise.resolve();
  }

  cancelCheckoutOwner() {
    return Promise.resolve();
  }
  retireOwner() {
    return Promise.resolve();
  }
}

class FakeSettings {
  onHostsChanged() {}
  onRecentChanged() {}
  recentSnapshot() {
    return Promise.resolve({recent: {version: 4, runs: []}, skipped: []});
  }
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

/** What the shell page exposes, read from inside it: the confinement the sandbox enforces. */
function inspectWelcomeContents(contents) {
  return requiredContents(contents, 'shell').executeJavaScript(
    `(() => {
      const style = getComputedStyle(document.documentElement);
      const width = Number.parseFloat(style.getPropertyValue('--chrome-inset-width')) || 0;
      const height = Number.parseFloat(style.getPropertyValue('--chrome-inset-height')) || 0;
      const visible = element => {
        const box = element.getBoundingClientRect();
        const computed = getComputedStyle(element);
        return computed.display !== 'none' && computed.visibility !== 'hidden' && box.width > 0 && box.height > 0;
      };
      // Controls and text drawn under the window's own controls (the inset the preload set).
      const drawn = Array.from(document.querySelectorAll('body *')).filter(element =>
        element.matches('a, button, input, select, textarea, [tabindex]:not([tabindex="-1"])') ||
        Array.from(element.childNodes).some(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim() !== ''));
      return {
        underChrome: drawn.flatMap(element => {
          const box = element.getBoundingClientRect();
          return visible(element) && box.left < width && box.top < height
            ? [element.tagName + ' ' + (element.textContent ?? '').trim().slice(0, 20)]
            : [];
        }),
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

registerAppScheme();
/** First loads of the pages created while the app starts (the app shell). */
const startupLoads = [];
let collectingStartupLoads = true;
/** Resolves, once the app has created every startup page, with all of their first loads. */
let startupCreated;
const startupPagesCreated = new Promise(resolve => {
  startupCreated = resolve;
});
app.on('web-contents-created', (_event, contents) => {
  confineContents(contents, log);
  if (collectingStartupLoads) startupLoads.push(firstLoad(contents));
});
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
    surfaceUrl: SHELL_URL,
    connect: (port, runId) => runs.connect(port, runId),
    closed: runId => runs.detach(runId),
    fatal: error => fatals.push(error.message),
  });
  const desktop = new Desktop({
    window: appWindow,
    welcome,
    hosts: new FakeHosts(),
    machines: {},
    runs,
    settings: new FakeSettings(),
    preferences: {
      answer: async () => ({
        appearance: {mode: 'system', light: 'paper', dark: 'ink'},
        notifications: {enabled: true},
        archived: [],
      }),
    },
    newRun: {},
    runWindows: {open: () => ({}), sendWelcome: () => undefined},
    shellUrl: SHELL_URL,
    welcomeIpc: {
      handle: (channel, listener) =>
        ipcMain.handle(channel, (event, ...args) =>
          listener(
            {
              senderId: event.sender.id,
              senderUrl: event.senderFrame?.url ?? null,
              rendererGeneration: rendererGeneration(event.senderFrame),
            },
            ...args,
          ),
        ),
    },
    log,
  });
  collectingStartupLoads = false;
  const loads = Promise.all(startupLoads);
  loads.catch(() => {});
  startupCreated({loads});
  void desktop;
});

globalThis.vibesysWelcomeRecoveryE2E = {
  /** Resolves once the app shell has finished its first load. */
  async startupPagesLoaded() {
    await (await startupPagesCreated).loads;
  },
  state() {
    // The control surface is installed before `app.whenReady()` finishes so Playwright can begin
    // polling immediately. Initialization may still be constructing AppWindow at that instant;
    // expose a total not-ready snapshot rather than turning an ordinary startup race into a
    // terminal evaluate error.
    if (appWindow === null) {
      return {
        currentWelcomeId: null,
        currentWelcomeUrl: null,
        logs: [...logs],
        fatals: [...fatals],
      };
    }
    const currentWelcomeId = ownedWelcomeId();
    return {
      currentWelcomeId,
      currentWelcomeUrl: ownedWelcomeContents()?.getURL() ?? null,
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
  /** The overview a document asks for through its bridge: the answer, or why it was refused. */
  overviewOf(contents) {
    return requiredContents(contents, 'shell').executeJavaScript(
      'window.vibesysWelcome.overview()',
      true,
    );
  },
  welcomeOverview() {
    return this.overviewOf(ownedWelcomeContents());
  },
  async openUnauthorizedWelcome() {
    unauthorizedWindow?.close();
    const holder = new BaseWindow({show: false, width: 960, height: 600});
    const view = sandboxedView('welcome.cjs');
    holder.contentView.addChildView(view);
    view.setBounds({x: 0, y: 0, width: 960, height: 600});
    unauthorizedWindow = holder;
    unauthorizedView = view;
    await view.webContents.loadURL(SHELL_URL);
    return {unauthorizedId: view.webContents.id, currentId: ownedWelcomeId()};
  },
  unauthorizedOverview() {
    return this.overviewOf(unauthorizedView?.webContents ?? null);
  },
  closeUnauthorizedWelcome() {
    unauthorizedWindow?.close();
    unauthorizedWindow = null;
    unauthorizedView = null;
  },
};
