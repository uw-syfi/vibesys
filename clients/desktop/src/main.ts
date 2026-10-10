/**
 * Electron main process: one window over a VibeSys web gateway that is already running.
 *
 * `scripts/run-desktop.sh` starts the gateway (locally or on an SSH host), forwards its port, and
 * passes the capability URL in `VIBESYS_DESKTOP_URL`. The shell only displays it: the window is
 * sandboxed and confined to the gateway origin, and the token never reaches a log line.
 */
import {fileURLToPath} from 'node:url';
import {app, BrowserWindow, Menu, nativeTheme, session, type WebContents} from 'electron';
import {
  isAllowedRequest,
  isAppUrl,
  type LaunchTarget,
  originOf,
  parseLaunchUrl,
} from './launch-url.js';
import {windowChrome} from './window-chrome.js';

/** The one browser permission the web UI uses (copying run IDs). */
const ALLOWED_PERMISSIONS: ReadonlySet<string> = new Set(['clipboard-sanitized-write']);

function log(message: string): void {
  console.error(`vibesys-desktop: ${message}`);
}

function guard(contents: WebContents, target: LaunchTarget): void {
  const stayInApp = (event: {readonly url: string; preventDefault(): void}): void => {
    if (isAppUrl(event.url, target.origin)) return;
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

function secureSession(target: LaunchTarget): void {
  const {defaultSession} = session;
  const urls = ['http://*/*', 'https://*/*', 'ws://*/*', 'wss://*/*'];
  defaultSession.webRequest.onBeforeRequest({urls}, (details, callback) => {
    const cancel = !isAllowedRequest(details.url, target.origin);
    if (cancel) log(`blocked a request to ${originOf(details.url)}`);
    callback({cancel});
  });
  defaultSession.setPermissionRequestHandler((_contents, permission, callback) =>
    callback(ALLOWED_PERMISSIONS.has(permission)),
  );
  defaultSession.setPermissionCheckHandler((_contents, permission) =>
    ALLOWED_PERMISSIONS.has(permission),
  );
}

function openWindow(target: LaunchTarget): void {
  // The web UI has one dark theme; make native menus and controls follow it.
  nativeTheme.themeSource = 'dark';
  const window = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 960,
    minHeight: 600,
    show: false,
    title: 'VibeSys',
    ...windowChrome(process.platform),
    webPreferences: {
      preload: fileURLToPath(new URL('./preload.cjs', import.meta.url)),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      webSecurity: true,
      spellcheck: false,
    },
  });
  window.once('ready-to-show', () => window.show());
  // The rejection message quotes the URL, which carries the token: report the origin only.
  window.loadURL(target.url).catch(() => log(`could not load the app from ${target.origin}`));
}

function main(): void {
  let target: LaunchTarget;
  try {
    target = parseLaunchUrl(process.env['VIBESYS_DESKTOP_URL']);
  } catch (error) {
    log((error as Error).message);
    process.exit(2);
  }
  // Child processes (none today) must not inherit the capability.
  delete process.env['VIBESYS_DESKTOP_URL'];
  app.on('web-contents-created', (_event, contents) => guard(contents, target));
  app.on('window-all-closed', () => app.quit());
  void app.whenReady().then(() => {
    secureSession(target);
    Menu.setApplicationMenu(Menu.buildFromTemplate(applicationMenu()));
    openWindow(target);
  });
}

function applicationMenu(): Electron.MenuItemConstructorOptions[] {
  const first: Electron.MenuItemConstructorOptions =
    process.platform === 'darwin' ? {role: 'appMenu'} : {role: 'fileMenu'};
  return [first, {role: 'editMenu'}, {role: 'viewMenu'}, {role: 'windowMenu'}];
}

main();
