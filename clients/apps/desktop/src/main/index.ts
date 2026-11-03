/**
 * Electron main process entry (`node` runtime): the composition root. It builds the connection
 * manager and the gateway calls, serves the gateway to the window over one MessagePort per call,
 * and opens the one window on the built UI with the hosts to read in its address. It calls no
 * `Effect.run*` of its own: the page owns every Effect program.
 */
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import {
  ConnectionManager,
  controlDirectory,
  installAskpass,
  type Link,
  LocalLink,
  SshLink,
} from '@vibesys/connection';
import type {HostId} from '@vibesys/hosts';
import {SettingsStore} from '@vibesys/settings';
import type {ScheduleTimeout} from '@vibesys/transport';
import {app, BrowserWindow, ipcMain, session, type WebContents} from 'electron';
import {createGatewayCalls, installGatewayIpc} from './gateway.js';
import {isOwnWindow, type PortEvent} from './port-ipc.js';
import {hostsFromArgv, isAppPage, startupHosts, windowUrl} from './window-policy.js';

/** `dist/renderer`: the built page, beside `dist/main`. */
const UI_DIRECTORY = fileURLToPath(new URL('../renderer/', import.meta.url));
const PRELOAD = fileURLToPath(new URL('../preload/index.cjs', import.meta.url));
/** The Go gateway binaries the build puts in `dist/gateway`. */
const GATEWAY_RESOURCES = fileURLToPath(new URL('../gateway/', import.meta.url));
/** Where the app keeps its files; tests point it at a scratch directory. */
const USER_DATA_ENV = 'VIBESYS_DESKTOP_USER_DATA';
/** How long quitting waits for the hosts to be released before exiting anyway. */
const RELEASE_BOUND_MS = 5_000;

const log = (message: string): void => {
  console.error(`vibesys-desktop: ${message}`);
};

const realTimers: ScheduleTimeout = (callback, ms) => {
  const timer = setTimeout(callback, ms);
  return () => clearTimeout(timer);
};

async function connectionManager(): Promise<ConnectionManager> {
  const controlPath = join(await controlDirectory(process.getuid?.() ?? 0), '%C');
  const askpass = await installAskpass(join(app.getPath('userData'), 'askpass.sh'));
  return new ConnectionManager({
    linkFor: (host: HostId): Link =>
      host.kind === 'local'
        ? new LocalLink()
        : new SshLink({alias: host.alias, controlPath, askpass, scheduleTimeout: realTimers}),
    appVersion: app.getVersion(),
    scheduleTimeout: realTimers,
    log,
  });
}

/** Keep `contents` on the built page and refuse every new window and every network request. */
function confine(contents: WebContents): void {
  const stay = (event: {readonly url: string; preventDefault(): void}): void => {
    if (isAppPage(UI_DIRECTORY, event.url)) return;
    event.preventDefault();
    log('blocked navigation away from the app');
  };
  contents.on('will-navigate', stay);
  contents.on('will-redirect', stay);
  contents.setWindowOpenHandler(() => {
    log('blocked a new window');
    return {action: 'deny'};
  });
}

function lockDownSession(): void {
  const {defaultSession} = session;
  defaultSession.webRequest.onBeforeRequest(
    {urls: ['http://*/*', 'https://*/*', 'ws://*/*', 'wss://*/*']},
    (_details, callback) => {
      log('blocked a network request');
      callback({cancel: true});
    },
  );
  defaultSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false));
  defaultSession.setPermissionCheckHandler(() => false);
}

/** Open the window on the built page, reading `hosts`. */
function openWindow(hosts: readonly string[]): BrowserWindow {
  const window = new BrowserWindow({
    width: 1280,
    height: 860,
    title: 'VibeSys',
    show: false,
    webPreferences: {
      preload: PRELOAD,
      sandbox: true,
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  window.once('ready-to-show', () => window.show());
  void window.loadURL(windowUrl(UI_DIRECTORY, hosts));
  return window;
}

async function start(): Promise<void> {
  const userData = process.env[USER_DATA_ENV];
  if (userData !== undefined && userData !== '') app.setPath('userData', userData);
  const asked = hostsFromArgv(process.argv);
  await app.whenReady();
  lockDownSession();
  const manager = await connectionManager();
  const settings = new SettingsStore({
    settingsPath: join(app.getPath('userData'), 'hosts.json'),
    recentPath: join(app.getPath('userData'), 'recent.json'),
    preferencesPath: join(app.getPath('userData'), 'preferences.json'),
    log,
  });
  const saved = Object.keys((await settings.hosts()).settings.hosts);
  const windows = new Set<number>();
  const calls = createGatewayCalls(manager, GATEWAY_RESOURCES, realTimers, log);
  installGatewayIpc(ipcMain, {
    calls,
    admits: (event: PortEvent) =>
      isOwnWindow(event, {
        has: id => windows.has(id),
        isAppUrl: url => isAppPage(UI_DIRECTORY, url),
      }),
  });
  app.on('web-contents-created', (_event, contents) => confine(contents));
  const window = openWindow(startupHosts(asked, saved));
  windows.add(window.webContents.id);
  window.on('closed', () => windows.delete(window.webContents.id));
  app.on('window-all-closed', () => app.quit());
  let releasing = false;
  app.on('will-quit', event => {
    if (releasing) return;
    releasing = true;
    event.preventDefault();
    const bound = new Promise<void>(resolve => realTimers(resolve, RELEASE_BOUND_MS));
    void Promise.race([manager[Symbol.asyncDispose](), bound]).finally(() => app.exit(0));
  });
}

void start().catch(error => {
  log(error instanceof Error ? error.message : String(error));
  app.exit(1);
});
