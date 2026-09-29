/**
 * Electron main process: one window over the app that `vibesys web home` serves.
 *
 * Owns the home server it starts (stopped on quit; a crash offers a restart), confines the
 * window to the app origin, and keeps the capability token out of every log line.
 */
import {join, resolve} from 'node:path';
import {
  app,
  BrowserWindow,
  dialog,
  Menu,
  type MenuItemConstructorOptions,
  nativeTheme,
  session,
  type WebContents,
} from 'electron';
import {type Home, type HomeEnd, startHome} from './home.js';
import {
  allowPermission,
  devOrigin,
  homeArguments,
  isAllowedRequest,
  isAppUrl,
  originOf,
  type QuitState,
  quitRequest,
  windowUrl,
} from './policy.js';

/** `clients/desktop` is the app path; the home server runs from the checkout around it. */
const REPOSITORY = resolve(app.getAppPath(), '../..');
/** Main is built as CommonJS next to the preload (electron.vite.config.ts). */
const PRELOAD = join(__dirname, '../preload/index.cjs');
/** Set by `electron-vite dev` to the Vite server of clients/web; unset for the built app. */
const RENDERER_URL = process.env['ELECTRON_RENDERER_URL'];
const DEV_ORIGIN = devOrigin(RENDERER_URL, import.meta.env.DEV);
/** The first `uv run` in a checkout syncs its environment before the server starts. */
const READY_TIMEOUT_MS = 60_000;
const STOP_GRACE_MS = 5_000;

/** The latest launch; null when it failed. Quit waits for it, so a starting server is stopped too. */
let home: Promise<Home | null> = Promise.resolve(null);
let win: BrowserWindow | null = null;
/** Where the window may navigate and send requests: the home server, or Vite in dev. */
let appOrigins: readonly string[] = [];
let quit: QuitState = 'running';

function log(message: string): void {
  console.error(`vibesys-desktop: ${message}`);
}

if (RENDERER_URL !== undefined && DEV_ORIGIN === null) {
  log(`ignoring ELECTRON_RENDERER_URL (${originOf(RENDERER_URL)}): not the dev Vite origin`);
}

function guard(contents: WebContents): void {
  const stayInApp = (event: {readonly url: string; preventDefault(): void}): void => {
    if (isAppUrl(event.url, appOrigins)) return;
    event.preventDefault();
    log(`blocked navigation to ${originOf(event.url)}`);
  };
  contents.on('will-navigate', stayInApp);
  contents.on('will-redirect', stayInApp);
  contents.setWindowOpenHandler(({url}) => {
    log(`blocked a new window for ${originOf(url)}`);
    return {action: 'deny'};
  });
}

function secureSession(): void {
  const {webRequest} = session.defaultSession;
  const urls = ['http://*/*', 'https://*/*', 'ws://*/*', 'wss://*/*'];
  webRequest.onBeforeRequest({urls}, (details, callback) => {
    const cancel = !isAllowedRequest(details.url, appOrigins);
    if (cancel) log(`blocked a request to ${originOf(details.url)}`);
    callback({cancel});
  });
  session.defaultSession.setPermissionRequestHandler((_contents, permission, callback) =>
    callback(allowPermission(permission)),
  );
  session.defaultSession.setPermissionCheckHandler((_contents, permission) =>
    allowPermission(permission),
  );
}

function menu(): Menu {
  const first: MenuItemConstructorOptions =
    process.platform === 'darwin' ? {role: 'appMenu'} : {role: 'fileMenu'};
  return Menu.buildFromTemplate([
    first,
    {role: 'editMenu'},
    {role: 'viewMenu'},
    {role: 'windowMenu'},
  ]);
}

function createWindow(): BrowserWindow {
  const window = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1024,
    minHeight: 640,
    show: false,
    title: 'VibeSys',
    titleBarStyle: 'hiddenInset',
    // The mockup's 40px sidebar head: 16px from the left edge, circles centred at y = 20.
    trafficLightPosition: {x: 16, y: 13},
    backgroundColor: nativeTheme.shouldUseDarkColors ? '#111113' : '#fcfcfd',
    webPreferences: {
      preload: PRELOAD,
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      webSecurity: true,
      spellcheck: false,
    },
  });
  window.once('ready-to-show', () => window.show());
  window.on('closed', () => {
    win = null;
  });
  return window;
}

async function launch(): Promise<Home | null> {
  try {
    return await startHome({
      command: [
        'uv',
        'run',
        'python',
        '-m',
        'entrypoints.web',
        'home',
        ...homeArguments(process.env['VIBESYS_HOME_PORT'], DEV_ORIGIN),
      ],
      cwd: REPOSITORY,
      env: process.env,
      readyTimeoutMs: READY_TIMEOUT_MS,
      stopGraceMs: STOP_GRACE_MS,
      onStderr: line => console.error(`[home] ${line}`),
    });
  } catch (error) {
    // A HomeStartError holds a headline and the server's stderr tail; neither carries the token.
    const detail = error instanceof Error ? error.message : String(error);
    log(detail.split('\n', 1)[0] ?? detail);
    if (quit === 'running') {
      dialog.showErrorBox('VibeSys home server unavailable', detail);
      app.quit();
    }
    return null;
  }
}

async function openHome(): Promise<void> {
  home = launch();
  const current = await home;
  if (current === null || quit !== 'running') return;
  let target: string;
  try {
    target = windowUrl(current, DEV_ORIGIN, process.env['VIBESYS_HOME_PORT']);
  } catch (error) {
    const detail = (error as Error).message;
    log(detail);
    dialog.showErrorBox('VibeSys home server unavailable', detail);
    app.quit();
    return;
  }
  void current.ended
    .then(onHomeEnded)
    .catch(error => log(`could not handle the home server's exit: ${(error as Error).message}`));
  appOrigins = [DEV_ORIGIN ?? current.origin];
  win ??= createWindow();
  // The rejection message quotes the URL, which carries the token: report the origin only.
  win.loadURL(target).catch(() => log(`could not load the app from ${appOrigins[0]}`));
}

async function onHomeEnded(end: HomeEnd): Promise<void> {
  switch (end.kind) {
    case 'stopped':
      return;
    case 'reused':
      // A home server started elsewhere (e.g. `vibesys web home --open`) rejects Vite's writes
      // with 403 forbidden_origin unless it was started with --dev-origin.
      if (DEV_ORIGIN !== null) {
        log(
          'reusing a home server that was started without --dev-origin; writes from Vite fail until it is restarted',
        );
      }
      return;
    case 'crashed': {
      log(`the home server stopped unexpectedly (${end.detail.split('\n', 1)[0]})`);
      const {response} = await dialog.showMessageBox({
        type: 'error',
        message: 'The VibeSys home server stopped.',
        detail: end.detail,
        buttons: ['Restart', 'Quit'],
        defaultId: 0,
        cancelId: 1,
      });
      if (response === 0 && quit === 'running') await openHome();
      else app.quit();
    }
  }
}

function main(): void {
  for (const signal of ['SIGINT', 'SIGTERM'] as const) process.on(signal, () => app.quit());
  app.on('second-instance', () => {
    if (quit !== 'running') return;
    // While the home server starts there is no window yet: open it now, empty, as feedback.
    win ??= createWindow();
    if (win.isMinimized()) win.restore();
    win.show();
    win.focus();
  });
  app.on('web-contents-created', (_event, contents) => guard(contents));
  app.on('window-all-closed', () => app.quit());
  app.on('before-quit', event => {
    const {hold, startStop} = quitRequest(quit);
    if (hold) event.preventDefault();
    if (!startStop) return;
    quit = 'stopping';
    void home
      .then(current => current?.stop())
      // stop() rejects only when signalling fails (e.g. EPERM); the message names no URL.
      .catch(error => log(`could not stop the home server: ${(error as Error).message}`))
      .finally(() => {
        quit = 'stopped';
        app.quit();
      });
  });
  void app.whenReady().then(() => {
    secureSession();
    Menu.setApplicationMenu(menu());
    return openHome();
  });
}

// One app per state home, as there is one home server per state home.
const stateHome = process.env['VIBESYS_STATE_HOME'];
if (stateHome !== undefined && stateHome !== '')
  app.setPath('userData', join(stateHome, 'desktop'));
if (app.requestSingleInstanceLock()) main();
else app.quit();
