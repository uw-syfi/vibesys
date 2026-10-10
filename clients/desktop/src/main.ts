/**
 * Electron main process: the host and run picker, and one window per attached run.
 *
 * Bundled mode (see `launch-args.ts`): every window loads pages shipped inside the app from
 * `app://vibesys` and has no network access. A run window's only way to its server is the preload
 * bridge: each `connect()` hands this process a MessagePort, which `relay` joins to a new byte
 * stream from the window's `Host` (this machine or an SSH host; the page cannot tell which). Each
 * run window has a `ConnectionSupervisor` that restores the host link after drops, sleep, and
 * network changes, then wakes the page so its own session resumes. The picker window is the
 * trusted place where hosts are chosen: its requests are answered only for picker windows, and
 * only for hosts the app offered.
 *
 * Gateway mode (`VIBESYS_DESKTOP_URL`, set by `scripts/run-desktop.sh`): the window displays a web
 * gateway that is already running, confined to its origin; the token never reaches a log line.
 */
import {readFile} from 'node:fs/promises';
import {homedir} from 'node:os';
import {join} from 'node:path';
import {fileURLToPath} from 'node:url';
import type {ScheduleTimeout} from '@vibesys/backend-client';
import {
  app,
  BrowserWindow,
  dialog,
  type IpcMainInvokeEvent,
  ipcMain,
  Menu,
  type MessagePortMain,
  nativeTheme,
  net,
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
import {controlDirectory, installAskpass} from './askpass.js';
import {type AttachedRun, checkAttachment, observedDial} from './attachment.js';
import {CONNECT_CHANNEL, WAKE_CHANNEL} from './bridge-protocol.js';
import {HostError} from './host.js';
import {HostPool, hostKey} from './host-pool.js';
import type {HostId} from './host-settings.js';
import {parseInstanceList, versionSkewMessage} from './instances.js';
import {type LaunchPlan, parseLaunch} from './launch-args.js';
import {isAllowedRequest, isAppUrl, type LaunchTarget, originOf} from './launch-url.js';
import {PICKER_CHANNELS, type PickerResult} from './picker-protocol.js';
import {type RelayPort, relay} from './relay.js';
import {
  type ConnectionStatus,
  ConnectionSupervisor,
  isTerminal,
  type StreamEnd,
} from './supervisor.js';
import {windowChrome} from './window-chrome.js';

/** The one browser permission the web UI uses (copying run IDs). */
const ALLOWED_PERMISSIONS: ReadonlySet<string> = new Set(['clipboard-sanitized-write']);
const NETWORK_URLS = ['http://*/*', 'https://*/*', 'ws://*/*', 'wss://*/*'];
/** `dist/ui`: the web UI bundle the build copies in, with the picker page beside it. */
const UI_ROOT = fileURLToPath(new URL('./ui/', import.meta.url));
const PICKER_URL = 'app://vibesys/picker.html';
/** The repository checkout this app was built in; `uv` runs VibeSys from it. */
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../', import.meta.url));
/** How often the main process looks for the network coming back. */
const NETWORK_POLL_MS = 5_000;

function log(message: string): void {
  console.error(`vibesys-desktop: ${message}`);
}

const realTimers: ScheduleTimeout = (callback, ms) => {
  const timer = setTimeout(callback, ms);
  return () => clearTimeout(timer);
};

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

function openWindow(url: string, describe: string, preload = 'preload.cjs'): BrowserWindow {
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
      preload: fileURLToPath(new URL(`./${preload}`, import.meta.url)),
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

function pythonCommand(): string[] {
  const configured = process.env['VIBESYS_PYTHON'];
  if (configured) return [configured];
  return ['uv', 'run', '--project', REPOSITORY_ROOT, 'python'];
}

/** What a run window's title says about its connection. */
function statusLabel(status: ConnectionStatus): string {
  switch (status.kind) {
    case 'connecting':
      return 'connecting';
    case 'connected':
      return 'connected';
    case 'reconnecting':
      return `reconnecting (attempt ${status.attempt})`;
    case 'offline':
      return 'offline, waiting for the network';
    case 'auth-needed':
      return 'sign-in needed';
    case 'run-ended':
      return 'run ended';
    case 'incompatible':
      return 'version mismatch';
    case 'failed':
      return 'cannot connect';
  }
}

function statusDetail(status: ConnectionStatus, hostName: string): string {
  switch (status.kind) {
    case 'auth-needed':
      return `${hostName} asked for credentials that were not given (${status.detail}). Retry to sign in again.`;
    case 'run-ended':
      return `The run on ${hostName} is no longer running. Its transcript stays on screen; open the picker to attach to another run.`;
    case 'offline':
    case 'incompatible':
    case 'failed':
      return status.detail;
    default:
      return '';
  }
}

interface RunBinding {
  readonly run: AttachedRun;
  readonly supervisor: ConnectionSupervisor;
}

/** The bundled app: the picker, run windows, their supervisors, and the hosts behind them. */
class DesktopApp {
  readonly #pool: HostPool;
  readonly #runs = new Map<number, RunBinding>();
  readonly #pickers = new Set<number>();
  #online = true;

  constructor(pool: HostPool) {
    this.#pool = pool;
  }

  get pool(): HostPool {
    return this.#pool;
  }

  openPicker(host: HostId | null): void {
    const query = host === null ? '' : `?host=${encodeURIComponent(hostKey(host))}`;
    const window = openWindow(`${PICKER_URL}${query}`, 'the picker', 'picker-preload.cjs');
    window.setSize(980, 640);
    const id = window.webContents.id;
    this.#pickers.add(id);
    window.on('closed', () => this.#pickers.delete(id));
  }

  /** Open a run window on `endpoint` of `host`, supervised. */
  async openRun(id: HostId, socketPath: string, instanceId: string | null): Promise<void> {
    const host = await this.#pool.host(id);
    const hostName = id.kind === 'local' ? 'This Mac' : id.alias;
    const run: AttachedRun = {
      host,
      hostName,
      vibesysCommand: await this.#pool.command(id),
      instanceId,
      endpoint: {socketPath},
    };
    const window = openWindow(APP_ENTRY_URL, 'the app bundle');
    const contentsId = window.webContents.id;
    let shown: ConnectionStatus['kind'] | null = null;
    const supervisor: ConnectionSupervisor = new ConnectionSupervisor({
      check: interactive => checkAttachment(run, interactive),
      scheduleTimeout: realTimers,
      wakePage: () => {
        if (!window.isDestroyed()) window.webContents.send(WAKE_CHANNEL);
      },
      show: status => {
        if (window.isDestroyed()) return;
        window.setTitle(`VibeSys · ${hostName} · ${statusLabel(status)}`);
        if (status.kind === shown) return;
        shown = status.kind;
        log(`${hostName}: ${statusLabel(status)}`);
        if (!isTerminal(status) && status.kind !== 'offline') return;
        void dialog
          .showMessageBox(window, {
            type: status.kind === 'run-ended' ? 'info' : 'warning',
            message: `${hostName}: ${statusLabel(status)}`,
            detail: statusDetail(status, hostName),
            buttons: ['Retry', 'Dismiss'],
            defaultId: 0,
            cancelId: 1,
          })
          .then(({response}) => {
            if (response === 0) supervisor.dispatch({type: 'user-retry'});
          });
      },
    });
    this.#runs.set(contentsId, {run, supervisor});
    window.on('closed', () => {
      this.#runs.delete(contentsId);
      supervisor.dispose();
    });
    window.setTitle(`VibeSys · ${hostName} · connecting`);
    supervisor.dispatch({type: 'start'});
  }

  /** Attach to registry run `instanceId` on `id`, refusing a version mismatch with its message. */
  async attachInstance(id: HostId, instanceId: string): Promise<void> {
    const host = await this.#pool.host(id);
    const listing = parseInstanceList(await host.invoke(['instances', 'list', '--json']));
    const record = listing.records.find(candidate =>
      candidate.kind === 'compatible'
        ? candidate.instance.id === instanceId
        : candidate.id === instanceId,
    );
    const hostName = id.kind === 'local' ? 'This Mac' : id.alias;
    if (record === undefined) throw new Error(`no live run ${instanceId} on ${hostName}`);
    if (record.kind === 'incompatible') {
      throw new Error(
        versionSkewMessage({
          hostName,
          vibesysCommand: await this.#pool.command(id),
          protocolVersion: record.protocolVersion,
          vibesysVersion: record.vibesysVersion,
        }),
      );
    }
    await this.openRun(id, record.instance.socketPath, instanceId);
  }

  /** Start a detached run on `id` in `project` and attach to it. */
  async startRun(id: HostId, project: string, args: readonly string[]): Promise<void> {
    const host = await this.#pool.host(id);
    const server = await host.startServer(['--project', project, ...args], {cwd: project});
    if (server.record.kind === 'incompatible') {
      const hostName = id.kind === 'local' ? 'This Mac' : id.alias;
      throw new Error(
        versionSkewMessage({
          hostName,
          vibesysCommand: await this.#pool.command(id),
          protocolVersion: server.record.protocolVersion,
          vibesysVersion: server.record.vibesysVersion,
        }),
      );
    }
    log(
      `started run ${server.record.instance.id}; stop it with: vibesys instances stop ${server.record.instance.id}`,
    );
    await this.openRun(id, server.endpoint.socketPath, server.record.instance.id);
  }

  /** Relay each run window's connections to its run; refuse every other sender. */
  bindConnections(): void {
    ipcMain.on(CONNECT_CHANNEL, event => {
      const [port] = event.ports;
      if (port === undefined) return;
      const binding = this.#runs.get(event.sender.id);
      if (binding === undefined || !isAppPage(event.senderFrame?.url ?? '')) {
        log('refused a connection from a window that is not bound to a run');
        port.close();
        return;
      }
      port.start();
      const report = (end: StreamEnd, detail: string): void =>
        binding.supervisor.dispatch({type: 'stream-ended', end, detail});
      void relay(relayPort(port), observedDial(binding.run, report));
    });
    // A machine that slept has dead connections nobody has noticed yet: check every link now.
    powerMonitor.on('resume', () => this.#broadcast('resumed'));
    this.#watchNetwork();
  }

  /** Answer the picker page's requests, for picker windows only. */
  bindPicker(): void {
    const handle = <T>(channel: string, answer: (...args: unknown[]) => Promise<T>): void => {
      ipcMain.handle(channel, async (event: IpcMainInvokeEvent, ...args: unknown[]) => {
        if (!this.#pickers.has(event.sender.id)) return failure(new Error('not a picker window'));
        try {
          return {ok: true, value: await answer(...args)} satisfies PickerResult<T>;
        } catch (error) {
          return failure(error);
        }
      });
    };
    handle(PICKER_CHANNELS.hosts, () => this.#pool.offered());
    handle(PICKER_CHANNELS.runs, async key => this.#pool.runs(await this.#host(key)));
    handle(PICKER_CHANNELS.signIn, async key => {
      await (await this.#pool.host(await this.#host(key))).ensureLink();
      return null;
    });
    handle(PICKER_CHANNELS.attach, async (key, instance) => {
      if (typeof instance !== 'string') throw new Error('pick a run to attach to');
      await this.attachInstance(await this.#host(key), instance);
      return null;
    });
    handle(PICKER_CHANNELS.start, async (key, project, args) => {
      if (typeof project !== 'string' || project.trim() === '')
        throw new Error('name a project path');
      const words = typeof args === 'string' ? args.split(/\s+/).filter(word => word !== '') : [];
      await this.startRun(await this.#host(key), project.trim(), words);
      return null;
    });
    handle(PICKER_CHANNELS.saveSettings, async (key, command) => {
      const id = await this.#host(key);
      if (id.kind !== 'ssh') throw new Error('this machine has no vibesys command setting');
      await this.#pool.saveCommand(id.alias, command);
      return null;
    });
  }

  async #host(key: unknown): Promise<HostId> {
    const id = await this.#pool.resolve(key);
    if (id === null) throw new Error('that host is not in ~/.ssh/config');
    return id;
  }

  #broadcast(type: 'resumed' | 'network-changed'): void {
    for (const {supervisor} of this.#runs.values()) supervisor.dispatch({type});
  }

  /** Report the network coming back (offline to online) to every supervisor. */
  #watchNetwork(): void {
    const poll = (): void => {
      const online = net.isOnline();
      if (online && !this.#online) this.#broadcast('network-changed');
      this.#online = online;
      realTimers(poll, NETWORK_POLL_MS);
    };
    realTimers(poll, NETWORK_POLL_MS);
  }
}

function failure(error: unknown): PickerResult<never> {
  return {
    ok: false,
    error: error instanceof Error ? error.message : String(error),
    authNeeded: error instanceof HostError && error.kind === 'auth',
  };
}

function runBundled(plan: Exclude<LaunchPlan, {kind: 'gateway'}>): void {
  protocol.registerSchemesAsPrivileged([
    {scheme: APP_SCHEME, privileges: {standard: true, secure: true}},
  ]);
  let desktop: DesktopApp | null = null;
  let released = false;
  app.on('will-quit', event => {
    if (released || desktop === null) return;
    event.preventDefault();
    // Runs are detached and keep going; quitting ends this app's streams and SSH masters only.
    void desktop.pool.closeAll().finally(() => {
      released = true;
      app.quit();
    });
  });
  for (const signal of ['SIGINT', 'SIGTERM'] as const) process.once(signal, () => app.quit());
  app.on('web-contents-created', (_event, contents) => guard(contents, isAppPage));
  app.on('window-all-closed', () => app.quit());
  void app.whenReady().then(async () => {
    // Pages reach servers only through the main process: no request leaves a window.
    secureSession(() => false);
    serveBundledUi();
    Menu.setApplicationMenu(Menu.buildFromTemplate(applicationMenu()));
    const userData = app.getPath('userData');
    const uid = process.getuid?.() ?? 0;
    desktop = new DesktopApp(
      new HostPool({
        sshConfigPath: join(homedir(), '.ssh', 'config'),
        settingsPath: join(userData, 'hosts.json'),
        controlPath: join(await controlDirectory(uid), '%C'),
        askpass: await installAskpass(join(userData, 'askpass.sh')),
        localPython: pythonCommand(),
        log,
      }),
    );
    desktop.bindConnections();
    desktop.bindPicker();
    try {
      await openPlan(desktop, plan);
    } catch (error) {
      log((error as Error).message);
      released = true;
      void desktop.pool.closeAll().finally(() => app.exit(1));
    }
  });
}

async function openPlan(
  desktop: DesktopApp,
  plan: Exclude<LaunchPlan, {kind: 'gateway'}>,
): Promise<void> {
  switch (plan.kind) {
    case 'picker':
      desktop.openPicker(plan.host);
      return;
    case 'instance':
      await desktop.attachInstance(plan.host, plan.instanceId);
      return;
    case 'start':
      log('starting a detached VibeSys run');
      await desktop.startRun({kind: 'local'}, plan.project, plan.runArgs);
      return;
    case 'attach':
      await desktop.openRun({kind: 'local'}, plan.socketPath, null);
      return;
  }
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
