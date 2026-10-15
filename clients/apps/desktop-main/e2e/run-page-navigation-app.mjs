/**
 * Real-Electron composition for navigating one window's run page from one run to another.
 *
 * Regression fixture for `AppWindow.openRunPage` (`clients/apps/desktop-main/src/main/window.ts`):
 * production owns `AppWindow` unchanged. This entry drives it directly, the way
 * `welcome-recovery-app.mjs` does, bypassing `RunControl` entirely — whether opening a second
 * run's page detaches the first run's binding before taking it over is `AppWindow`'s own decision,
 * not anything `RunControl` adds, so a `RunControl`/host-connection fixture would only add
 * irrelevant machinery between the test and the code under test.
 */
import {app, webContents} from 'electron';

const [{confineContents, lockDownSession, registerAppScheme, serveBundledUi}, {AppWindow}] =
  await Promise.all([
    import(new URL('../dist/main/app-security.js', import.meta.url)),
    import(new URL('../dist/main/window.js', import.meta.url)),
  ]);

const RUN_URL = 'app://vibesys/desktop.html';

/** The two runs this fixture opens in turn, in the one window. */
const RUNS = {
  alpha: {runId: 'run-alpha', hostLabel: 'Alpha Host'},
  bravo: {runId: 'run-bravo', hostLabel: 'Bravo Host'},
};

const logs = [];
const fatals = [];
/** Every `runId` `AppWindowDeps.closed` has reported, in the order it reported them. */
const closedRunIds = [];
let appWindow = null;

function log(message) {
  logs.push(message);
}

registerAppScheme();
app.on('web-contents-created', (_event, contents) => confineContents(contents, log));
app.on('window-all-closed', () => app.quit());

// See `welcome-recovery-app.mjs`: schedule work after this module finishes evaluating, so
// Playwright's substituted `app.whenReady()` is not awaited at top level.
void app.whenReady().then(() => {
  lockDownSession(log);
  serveBundledUi();
  appWindow = new AppWindow({
    log,
    connect: () => {},
    closed: runId => closedRunIds.push(runId),
    fatal: error => fatals.push(error.message),
  });
});

globalThis.vibesysRunPageNavigationE2E = {
  state() {
    // Exposed before `app.whenReady()` finishes so Playwright can begin polling immediately;
    // `ready` lets the spec wait out that startup race on a control method that never throws.
    return {ready: appWindow !== null, logs: [...logs], fatals: [...fatals]};
  },
  openRun(label) {
    const target = RUNS[label];
    if (target === undefined) throw new Error(`no such run: ${label}`);
    if (appWindow === null) throw new Error('the app window is unavailable');
    appWindow.openRunPage(target.runId, {hostLabel: target.hostLabel, task: null});
  },
  windowMode() {
    return appWindow === null ? null : appWindow.mode;
  },
  closedRunIds() {
    return [...closedRunIds];
  },
  runWebContentsIds() {
    return webContents
      .getAllWebContents()
      .filter(contents => contents.getURL() === RUN_URL && !contents.isDestroyed())
      .map(contents => contents.id);
  },
};
