/**
 * Electron main process: one window that opens on the welcome view and becomes the run view.
 *
 * Bundled mode (see `launch-args.ts`): every view loads pages shipped inside the app from
 * `app://vibesys` and has no network access. The window holds two views. The welcome view
 * (`welcome.html`, its own narrow preload) is the trusted place where hosts, checkouts, projects,
 * and tasks are chosen: its requests are answered only for its own web contents, and every value is
 * validated here before it reaches a host. The run view (the web UI, `desktop.html`) names no
 * host, path, or command: its only way to its server is the preload bridge, where each `connect()`
 * hands this process a MessagePort that `relay` joins to a new byte stream from the run's `Host`.
 * While a run is shown, the welcome view shrinks to the title strip above it, showing the host and
 * the connection status; clicking the host brings the welcome view back. A `ConnectionSupervisor`
 * restores the host link after drops, sleep, and network changes, then wakes the page so its own
 * session resumes.
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
  BaseWindow,
  BrowserWindow,
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
  WebContentsView,
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
import {HostPool, systemHostFactory} from './host-pool.js';
import {type HostId, hostFromKey, hostKey, hostLabel, validHostPath} from './host-settings.js';
import {versionSkewMessage} from './instances.js';
import {type LaunchPlan, parseLaunch} from './launch-args.js';
import {isAllowedRequest, isAppUrl, type LaunchTarget, originOf} from './launch-url.js';
import {recentStatus} from './recent.js';
import {type RelayPort, relay} from './relay.js';
import {shellWords} from './shell-words.js';
import {
  type ConnectionStatus,
  ConnectionSupervisor,
  isTerminal,
  type StreamEnd,
} from './supervisor.js';
import {suggestCheckout, suggestProjects} from './welcome-model.js';
import {
  CHROME_CHANNEL,
  type ChromeState,
  WELCOME_CHANNELS,
  type WelcomeHost,
  type WelcomeOverview,
  type WelcomeRecentStatus,
  type WelcomeResult,
} from './welcome-protocol.js';
import {TITLEBAR_HEIGHT, windowChrome} from './window-chrome.js';

/** The one browser permission the web UI uses (copying run IDs). */
const ALLOWED_PERMISSIONS: ReadonlySet<string> = new Set(['clipboard-sanitized-write']);
const NETWORK_URLS = ['http://*/*', 'https://*/*', 'ws://*/*', 'wss://*/*'];
/** `dist/ui`: the web UI bundle the build copies in, with the welcome page beside it. */
const UI_ROOT = fileURLToPath(new URL('./ui/', import.meta.url));
const WELCOME_URL = 'app://vibesys/welcome.html';
/** The repository checkout this app was built in: This Mac's VibeSys checkout by default. */
const REPOSITORY_ROOT = fileURLToPath(new URL('../../../', import.meta.url)).replace(/\/+$/, '');
/** How often the main process looks for the network coming back. */
const NETWORK_POLL_MS = 5_000;

function log(message: string): void {
  console.error(`vibesys-desktop: ${message}`);
}

const realTimers: ScheduleTimeout = (callback, ms) => {
  const timer = setTimeout(callback, ms);
  return () => clearTimeout(timer);
};

/** Keep every view on `allowed` pages and refuse every new window. */
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

const WEB_PREFERENCES = {
  contextIsolation: true,
  sandbox: true,
  nodeIntegration: false,
  webSecurity: true,
  spellcheck: false,
} as const;

/** A sandboxed view with `preload` as its only way out. */
function view(preload: string): WebContentsView {
  return new WebContentsView({
    webPreferences: {
      ...WEB_PREFERENCES,
      preload: fileURLToPath(new URL(`./${preload}`, import.meta.url)),
    },
  });
}

/** The gateway window: one page on the gateway's origin. */
function openGatewayWindow(url: string, describe: string): BrowserWindow {
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
      ...WEB_PREFERENCES,
      preload: fileURLToPath(new URL('./preload.cjs', import.meta.url)),
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
    openGatewayWindow(target.url, target.origin);
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

/** What the title strip says about the connection. */
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
      return `The run on ${hostName} is no longer running. Its transcript stays on screen; click the host to attach to another run.`;
    case 'offline':
    case 'incompatible':
    case 'failed':
      return status.detail;
    default:
      return '';
  }
}

interface RunBinding {
  readonly view: WebContentsView;
  readonly run: AttachedRun;
  readonly supervisor: ConnectionSupervisor;
  readonly project: string;
  status: ConnectionStatus;
}

/**
 * The bundled app's one window: the welcome view (trusted, `welcome-preload`) and, once attached,
 * the run view (the web UI, `preload`) under the welcome view's title strip. The main process owns
 * the hosts, the attached run's supervisor, and which view fills the window.
 */
class DesktopApp {
  readonly #pool: HostPool;
  readonly #window: BaseWindow;
  readonly #welcome: WebContentsView;
  #run: RunBinding | null = null;
  #mode: 'welcome' | 'run' = 'welcome';
  #online = true;

  constructor(pool: HostPool) {
    this.#pool = pool;
    this.#window = new BaseWindow({
      width: 1440,
      height: 900,
      minWidth: 960,
      minHeight: 600,
      title: 'VibeSys',
      ...windowChrome(process.platform),
    });
    this.#welcome = view('welcome-preload.cjs');
    this.#window.contentView.addChildView(this.#welcome);
    this.#window.on('resize', () => this.#layout());
    this.#window.on('closed', () => this.#detach());
    this.#layout();
    this.#welcome.webContents
      .loadURL(WELCOME_URL)
      .catch(() => log('could not load the welcome view'));
  }

  get pool(): HostPool {
    return this.#pool;
  }

  /** Show the welcome view, on `host`'s panel when given. */
  showWelcome(host: HostId | null): void {
    if (host !== null) {
      void this.#welcome.webContents.executeJavaScript(
        `window.location.hash = ${JSON.stringify(hostKey(host))}`,
      );
    }
    this.#setMode('welcome');
  }

  #setMode(mode: 'welcome' | 'run'): void {
    this.#mode = this.#run === null ? 'welcome' : mode;
    this.#layout();
    this.#sendChrome();
  }

  /** Lay the views out: the filling view, and the welcome view's strip over a run. */
  #layout(): void {
    if (this.#window.isDestroyed()) return;
    const {width, height} = this.#window.getContentBounds();
    const full = {x: 0, y: 0, width, height};
    // The run page follows one dark theme; the welcome view follows the system's.
    nativeTheme.themeSource = this.#mode === 'run' ? 'dark' : 'system';
    if (this.#run !== null) {
      this.#run.view.setBounds(full);
      this.#run.view.setVisible(this.#mode === 'run');
    }
    this.#welcome.setBounds(this.#mode === 'run' ? {...full, height: TITLEBAR_HEIGHT} : full);
    // Re-adding moves the welcome view (the title strip) above the run view.
    this.#window.contentView.addChildView(this.#welcome);
  }

  chrome(): ChromeState {
    const run = this.#run;
    return {
      mode: this.#mode,
      attached:
        run === null
          ? null
          : {
              hostLabel: run.run.hostName,
              project: run.project,
              status: statusLabel(run.status),
              detail: statusDetail(run.status, run.run.hostName),
              stuck: isTerminal(run.status),
            },
    };
  }

  #sendChrome(): void {
    if (!this.#welcome.webContents.isDestroyed()) {
      this.#welcome.webContents.send(CHROME_CHANNEL, this.chrome());
    }
  }

  /** End the attached run's view and supervisor; the run itself keeps going on its host. */
  #detach(): void {
    const run = this.#run;
    if (run === null) return;
    this.#run = null;
    run.supervisor.dispose();
    if (!this.#window.isDestroyed()) this.#window.contentView.removeChildView(run.view);
    run.view.webContents.close();
  }

  /** Show the run on `socketPath` of `id` in the window, supervised, replacing any other. */
  async openRun(
    id: HostId,
    socketPath: string,
    instanceId: string | null,
    project: string,
  ): Promise<void> {
    const host = await this.#pool.host(id);
    const hostName = hostLabel(id);
    const run: AttachedRun = {
      host,
      hostName,
      vibesysCommand: await this.#pool.command(id),
      instanceId,
      endpoint: {socketPath},
    };
    this.#detach();
    const runView = view('preload.cjs');
    const supervisor: ConnectionSupervisor = new ConnectionSupervisor({
      check: interactive => checkAttachment(run, interactive),
      scheduleTimeout: realTimers,
      wakePage: () => {
        if (!runView.webContents.isDestroyed()) runView.webContents.send(WAKE_CHANNEL);
      },
      show: status => {
        if (this.#run?.supervisor !== supervisor) return;
        const changed = status.kind !== this.#run.status.kind;
        this.#run.status = status;
        this.#window.setTitle(`VibeSys · ${hostName} · ${statusLabel(status)}`);
        this.#sendChrome();
        if (changed) log(`${hostName}: ${statusLabel(status)}`);
      },
    });
    this.#run = {
      view: runView,
      run,
      supervisor,
      project,
      status: {kind: 'connecting'},
    };
    this.#window.contentView.addChildView(runView);
    this.#window.setTitle(`VibeSys · ${hostName} · connecting`);
    runView.webContents.loadURL(APP_ENTRY_URL).catch(() => log('could not load the run view'));
    this.#setMode('run');
    supervisor.dispatch({type: 'start'});
  }

  /** Attach to registry run `instanceId` on `id`, refusing a version mismatch with its message. */
  async attachInstance(id: HostId, instanceId: string, task: string | null = null): Promise<void> {
    const records = await this.#pool.records(id);
    const record = records.find(candidate =>
      candidate.kind === 'compatible'
        ? candidate.instance.id === instanceId
        : candidate.id === instanceId,
    );
    const hostName = hostLabel(id);
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
    const {instance} = record;
    await this.#remember(id, instance.projectRoot, task, instance.id, instance.runId);
    await this.openRun(id, instance.socketPath, instanceId, instance.projectRoot);
  }

  /** Start a detached run of `task` on `id` in `project` and attach to it. */
  async startRun(
    id: HostId,
    project: string,
    task: string | null,
    args: readonly string[],
  ): Promise<void> {
    await this.#launch(id, project, task, [...(task === null ? [] : ['--task', task]), ...args]);
  }

  /** Resume stopped run `run` (the latest when empty) of `project` on `id`, detached, and attach. */
  async resumeRun(id: HostId, project: string, run: string): Promise<void> {
    await this.#launch(id, project, null, ['--resume', ...(run === '' ? [] : [run])]);
  }

  async #launch(
    id: HostId,
    project: string,
    task: string | null,
    args: readonly string[],
  ): Promise<void> {
    const host = await this.#pool.host(id);
    const server = await host.startServer(args, {cwd: project});
    if (server.record.kind === 'incompatible') {
      throw new Error(
        versionSkewMessage({
          hostName: hostLabel(id),
          vibesysCommand: await this.#pool.command(id),
          protocolVersion: server.record.protocolVersion,
          vibesysVersion: server.record.vibesysVersion,
        }),
      );
    }
    const {instance} = server.record;
    log(
      server.alreadyLive
        ? `the run is already live as ${instance.id}; attaching to it`
        : `started run ${instance.id}; stop it with: vibesys instances stop ${instance.id}`,
    );
    await this.#remember(id, instance.projectRoot, task, instance.id, instance.runId);
    await this.openRun(id, server.endpoint.socketPath, instance.id, instance.projectRoot);
  }

  async #remember(
    id: HostId,
    project: string,
    task: string | null,
    instanceId: string,
    runId: string | null,
  ): Promise<void> {
    try {
      await this.#pool.remember({
        host: hostKey(id),
        project,
        task,
        instanceId,
        runId,
        attachedAt: Date.now(),
      });
    } catch (error) {
      log(`could not remember the run: ${(error as Error).message}`);
    }
  }

  /** Relay the run view's connections to its run; refuse every other sender. */
  bindConnections(): void {
    ipcMain.on(CONNECT_CHANNEL, event => {
      const [port] = event.ports;
      if (port === undefined) return;
      const binding = this.#run;
      if (
        binding === null ||
        event.sender.id !== binding.view.webContents.id ||
        !isAppPage(event.senderFrame?.url ?? '')
      ) {
        log('refused a connection from a view that is not bound to a run');
        port.close();
        return;
      }
      port.start();
      const report = (end: StreamEnd, detail: string): void =>
        binding.supervisor.dispatch({type: 'stream-ended', end, detail});
      void relay(relayPort(port), observedDial(binding.run, report));
    });
    // A machine that slept has dead connections nobody has noticed yet: check the link now.
    powerMonitor.on('resume', () => this.#run?.supervisor.dispatch({type: 'resumed'}));
    this.#watchNetwork();
  }

  /** Answer the welcome view's requests, for the welcome view only. */
  bindWelcome(): void {
    this.#bindQueries();
    this.#bindActions();
  }

  /** Answer `channel` with `answer`, for the welcome view's own web contents only. */
  #handle<T>(channel: string, answer: (...args: unknown[]) => Promise<T>): void {
    ipcMain.handle(channel, async (event: IpcMainInvokeEvent, ...args: unknown[]) => {
      if (
        event.sender.id !== this.#welcome.webContents.id ||
        event.senderFrame?.url.startsWith(WELCOME_URL) !== true
      ) {
        return failure(new Error('not the welcome view'));
      }
      try {
        return {ok: true, value: await answer(...args)} satisfies WelcomeResult<T>;
      } catch (error) {
        return failure(error);
      }
    });
  }

  /** What the welcome view reads: the overview, recent statuses, hosts, and one host's panel. */
  #bindQueries(): void {
    const handle = this.#handle.bind(this);
    const pool = this.#pool;
    handle(WELCOME_CHANNELS.overview, async () => {
      const recent = await pool.recent();
      return {
        settingsProblem: await pool.settingsCheck(),
        recent: recent.runs.map(run => ({...run, hostLabel: hostLabel(hostFromKey(run.host))})),
        chrome: this.chrome(),
      } satisfies WelcomeOverview;
    });
    handle(WELCOME_CHANNELS.recentStatus, async key => {
      const id = hostFromKey(key);
      const runs = (await pool.recent()).runs.filter(run => run.host === hostKey(id));
      let records: Awaited<ReturnType<HostPool['records']>> | {error: string};
      try {
        records = await pool.records(id);
      } catch (error) {
        records = {error: (error as Error).message};
      }
      const statuses: Record<string, WelcomeRecentStatus> = {};
      for (const run of runs) statuses[run.instanceId] = recentStatus(run, records);
      return statuses;
    });
    handle(WELCOME_CHANNELS.hosts, () => pool.aliases());
    handle(WELCOME_CHANNELS.host, async key => {
      const id = hostFromKey(key);
      const checkout = await pool.checkout(id);
      const recent = (await pool.recent()).runs;
      if (checkout === null) {
        return {
          key: hostKey(id),
          label: hostLabel(id),
          checkout,
          suggestedCheckout: await pool.suggestedCheckout(id),
          runs: [],
          projects: suggestProjects([], recent, hostKey(id)),
        } satisfies WelcomeHost;
      }
      const records = await pool.records(id);
      return {
        key: hostKey(id),
        label: hostLabel(id),
        checkout,
        suggestedCheckout: suggestCheckout(records),
        runs: await pool.runs(id, records),
        projects: suggestProjects(records, recent, hostKey(id)),
      } satisfies WelcomeHost;
    });
  }

  /** What the welcome view does: set a checkout, sign in, list tasks, attach, start, resume. */
  #bindActions(): void {
    const handle = this.#handle.bind(this);
    const pool = this.#pool;
    handle(WELCOME_CHANNELS.setCheckout, async (key, path) =>
      pool.setCheckout(hostFromKey(key), path),
    );
    handle(WELCOME_CHANNELS.signIn, async key => {
      await pool.signIn(hostFromKey(key));
      return null;
    });
    handle(WELCOME_CHANNELS.tasks, async (key, project) => pool.tasks(hostFromKey(key), project));
    handle(WELCOME_CHANNELS.attach, async (key, instance) => {
      if (typeof instance !== 'string' || !/^[0-9a-f]{12}$/.test(instance)) {
        throw new Error('pick a run to attach to');
      }
      const id = hostFromKey(key);
      const known = (await pool.recent()).runs.find(
        run => run.host === hostKey(id) && run.instanceId === instance,
      );
      await this.attachInstance(id, instance, known?.task ?? null);
      return null;
    });
    handle(WELCOME_CHANNELS.start, async (key, project, task, args) => {
      if (typeof task !== 'string' || !/^[a-z0-9][a-z0-9._-]{0,127}$/.test(task)) {
        throw new Error("pick one of the project's tasks");
      }
      const words = typeof args === 'string' ? shellWords(args) : [];
      await this.startRun(
        hostFromKey(key),
        validHostPath(project, 'the project directory'),
        task,
        words,
      );
      return null;
    });
    handle(WELCOME_CHANNELS.resume, async (key, project, run) => {
      const runId = typeof run === 'string' ? run.trim() : '';
      if (!/^[A-Za-z0-9._-]*$/.test(runId) || runId.startsWith('-')) {
        throw new Error('a run id is letters, digits, ".", "_", and "-", not starting with "-"');
      }
      await this.resumeRun(
        hostFromKey(key),
        validHostPath(project, 'the project directory'),
        runId,
      );
      return null;
    });
    handle(WELCOME_CHANNELS.showRun, async () => {
      this.#setMode('run');
      return null;
    });
    handle(WELCOME_CHANNELS.showWelcome, async () => {
      this.#setMode('welcome');
      return null;
    });
    handle(WELCOME_CHANNELS.retry, async () => {
      this.#run?.supervisor.dispatch({type: 'user-retry'});
      return null;
    });
  }

  /** Report the network coming back (offline to online) to the supervisor. */
  #watchNetwork(): void {
    const poll = (): void => {
      const online = net.isOnline();
      if (online && !this.#online) this.#run?.supervisor.dispatch({type: 'network-changed'});
      this.#online = online;
      realTimers(poll, NETWORK_POLL_MS);
    };
    realTimers(poll, NETWORK_POLL_MS);
  }
}

function failure(error: unknown): WelcomeResult<never> {
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
    // Pages reach servers only through the main process: no request leaves a view.
    secureSession(() => false);
    serveBundledUi();
    Menu.setApplicationMenu(Menu.buildFromTemplate(applicationMenu()));
    const userData = app.getPath('userData');
    const uid = process.getuid?.() ?? 0;
    desktop = new DesktopApp(
      new HostPool({
        sshConfigPath: join(homedir(), '.ssh', 'config'),
        settingsPath: join(userData, 'hosts.json'),
        recentPath: join(userData, 'recent.json'),
        localCheckout: REPOSITORY_ROOT,
        factory: systemHostFactory({
          controlPath: join(await controlDirectory(uid), '%C'),
          askpass: await installAskpass(join(userData, 'askpass.sh')),
        }),
        log,
      }),
    );
    desktop.bindConnections();
    desktop.bindWelcome();
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
    case 'welcome':
      desktop.showWelcome(plan.host);
      return;
    case 'instance':
      await desktop.attachInstance(plan.host, plan.instanceId);
      return;
    case 'start':
      log('starting a detached VibeSys run');
      await desktop.startRun({kind: 'local'}, plan.project, null, plan.runArgs);
      return;
    case 'attach':
      await desktop.openRun({kind: 'local'}, plan.socketPath, null, '');
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
