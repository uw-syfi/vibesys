/**
 * Electron main process: one window bound to one VibeSys server.
 *
 * Bundled mode (`--project PATH` or `--socket PATH`, see `launch-args.ts`): the window loads the
 * web UI shipped inside the app from `app://vibesys` and has no network access. Its only way to
 * the server is the preload bridge: each `connect()` hands this process a MessagePort, which
 * `relay` joins to a new byte stream from the window's `Host`. The page never learns where the
 * server is, so a local server and (later) a remote one look the same to it.
 *
 * Gateway mode (`VIBESYS_DESKTOP_URL`, set by `scripts/run-desktop.sh`): the window displays a web
 * gateway that is already running, confined to its origin; the token never reaches a log line.
 */
import {readFile} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {
  app,
  BrowserWindow,
  ipcMain,
  Menu,
  type MessagePortMain,
  nativeTheme,
  powerMonitor,
  protocol,
  session,
  type WebContents,
} from 'electron';
import {
  APP_ENTRY_URL,
  APP_SCHEME,
  assetPath,
  CONTENT_SECURITY_POLICY,
  contentType,
  isAppPage,
} from './app-assets.js';
import {CONNECT_CHANNEL, WAKE_CHANNEL} from './bridge-protocol.js';
import type {Endpoint, Host} from './host.js';
import {type LaunchPlan, parseLaunch} from './launch-args.js';
import {isAllowedRequest, isAppUrl, type LaunchTarget, originOf} from './launch-url.js';
import {LocalHost} from './local-host.js';
import {type RelayPort, relay} from './relay.js';
import {windowChrome} from './window-chrome.js';

/** The one browser permission the web UI uses (copying run IDs). */
const ALLOWED_PERMISSIONS: ReadonlySet<string> = new Set(['clipboard-sanitized-write']);
const NETWORK_URLS = ['http://*/*', 'https://*/*', 'ws://*/*', 'wss://*/*'];
/** `dist/ui`: the web UI bundle the build copies in. */
const UI_ROOT = fileURLToPath(new URL('./ui/', import.meta.url));
/** The repository checkout this app was built in; `uv` runs VibeSys from it. */
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../', import.meta.url));

function log(message: string): void {
  console.error(`vibesys-desktop: ${message}`);
}

/** Keep every window on `allowed` pages and refuse every new window. */
function guard(contents: WebContents, allowed: (url: string) => boolean): void {
  const stay = (event: {readonly url: string; preventDefault(): void}): void => {
    if (allowed(event.url)) return;
    event.preventDefault();
    log(`blocked navigation to ${originOf(event.url)}`);
  };
  contents.on('will-navigate', stay);
  contents.on('will-redirect', stay);
  contents.setWindowOpenHandler(({url}) => {
    log(`blocked a new window for ${originOf(url)}`);
    return {action: 'deny'};
  });
}

/** Allow the page only the network requests `allowed` accepts, and only the clipboard. */
function secureSession(allowed: (url: string) => boolean): void {
  const {defaultSession} = session;
  defaultSession.webRequest.onBeforeRequest({urls: NETWORK_URLS}, (details, callback) => {
    const cancel = !allowed(details.url);
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

function openWindow(url: string, describe: string): BrowserWindow {
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
  // A rejection message quotes the URL, which may carry a token: report `describe` only.
  window.loadURL(url).catch(() => log(`could not load the app from ${describe}`));
  return window;
}

function applicationMenu(): Electron.MenuItemConstructorOptions[] {
  const first: Electron.MenuItemConstructorOptions =
    process.platform === 'darwin' ? {role: 'appMenu'} : {role: 'fileMenu'};
  return [first, {role: 'editMenu'}, {role: 'viewMenu'}, {role: 'windowMenu'}];
}

function runGateway(target: LaunchTarget): void {
  app.on('web-contents-created', (_event, contents) =>
    guard(contents, url => isAppUrl(url, target.origin)),
  );
  app.on('window-all-closed', () => app.quit());
  void app.whenReady().then(() => {
    secureSession(url => isAllowedRequest(url, target.origin));
    Menu.setApplicationMenu(Menu.buildFromTemplate(applicationMenu()));
    openWindow(target.url, target.origin);
  });
}

/** Serve the bundled UI on `app://vibesys`, with a policy that forbids every connection. */
function serveBundledUi(): void {
  protocol.handle(APP_SCHEME, async request => {
    const file = assetPath(request.url, UI_ROOT);
    const type = file === null ? null : contentType(file);
    if (file === null || type === null) return new Response('Not found', {status: 404});
    let body: Buffer;
    try {
      body = await readFile(file);
    } catch {
      return new Response('Not found', {status: 404});
    }
    return new Response(new Uint8Array(body), {
      headers: {
        'Content-Type': type,
        'Content-Security-Policy': CONTENT_SECURITY_POLICY,
        'X-Content-Type-Options': 'nosniff',
      },
    });
  });
}

function relayPort(port: MessagePortMain): RelayPort {
  return {
    post: message => port.postMessage(message),
    onMessage: listener => port.on('message', event => listener(event.data)),
    onClose: listener => port.on('close', listener),
    close: () => port.close(),
  };
}

/** Join every connection a bundled page opens to a new stream to `endpoint` on `host`. */
function bindConnections(host: Host, endpoint: Endpoint): void {
  ipcMain.on(CONNECT_CHANNEL, event => {
    const [port] = event.ports;
    if (port === undefined) return;
    if (!isAppPage(event.senderFrame?.url ?? '')) {
      log('refused a connection from a page outside the app');
      port.close();
      return;
    }
    port.start();
    void relay(relayPort(port), () => host.dial(endpoint));
  });
  // A machine that slept has dead connections the page has not noticed yet: tell it to retry now.
  powerMonitor.on('resume', () => {
    for (const window of BrowserWindow.getAllWindows()) window.webContents.send(WAKE_CHANNEL);
  });
}

function pythonCommand(): string[] {
  const configured = process.env['VIBESYS_PYTHON'];
  if (configured) return [configured];
  return ['uv', 'run', '--project', REPOSITORY_ROOT, 'python'];
}

function runBundled(plan: Exclude<LaunchPlan, {kind: 'gateway'}>): void {
  protocol.registerSchemesAsPrivileged([
    {scheme: APP_SCHEME, privileges: {standard: true, secure: true}},
  ]);
  const host = new LocalHost({python: pythonCommand()});
  let released = false;
  app.on('will-quit', event => {
    if (released) return;
    event.preventDefault();
    void host.close().finally(() => {
      released = true;
      app.quit();
    });
  });
  for (const signal of ['SIGINT', 'SIGTERM'] as const) process.once(signal, () => app.quit());
  app.on('web-contents-created', (_event, contents) => guard(contents, isAppPage));
  app.on('window-all-closed', () => app.quit());
  void app.whenReady().then(async () => {
    // The page reaches its server only through the bridge: no request leaves the window.
    secureSession(() => false);
    serveBundledUi();
    Menu.setApplicationMenu(Menu.buildFromTemplate(applicationMenu()));
    let endpoint: Endpoint;
    if (plan.kind === 'attach') {
      endpoint = {socketPath: plan.socketPath};
    } else {
      log('starting the VibeSys server');
      try {
        const server = await host.startServer(plan.serverArgs);
        endpoint = server.endpoint;
        void server.exited.then(exit => log(`the server exited (${exit.code ?? 'signal'})`));
      } catch (error) {
        log((error as Error).message);
        released = true;
        void host.close().finally(() => app.exit(1));
        return;
      }
    }
    bindConnections(host, endpoint);
    openWindow(APP_ENTRY_URL, 'the app bundle');
  });
}

function main(): void {
  let plan: LaunchPlan;
  try {
    const argv = process.argv.slice(app.isPackaged ? 1 : 2);
    plan = parseLaunch(argv, process.env['VIBESYS_DESKTOP_URL'], process.env['INIT_CWD'] ?? '.');
  } catch (error) {
    log((error as Error).message);
    process.exit(2);
  }
  // Child processes must not inherit the capability.
  delete process.env['VIBESYS_DESKTOP_URL'];
  if (plan.kind === 'gateway') runGateway(plan.target);
  else runBundled(plan);
}

main();
