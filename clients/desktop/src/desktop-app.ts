/**
 * The bundled app's one window: the welcome view (trusted, `welcome-preload`) and, once attached,
 * the run view (the web UI, `preload`) under the welcome view's title strip.
 *
 * The welcome view is where hosts, checkouts, projects, and tasks are chosen; it reaches this
 * process only through `welcome-ipc.ts`, which validates every request. The run view names no
 * host, path, or command: its only way to its server is the preload bridge, where each `connect()`
 * hands this process a MessagePort that `relay` joins to a new byte stream from the run's `Host`.
 * A `ConnectionSupervisor` restores the host link after drops, sleep, and network changes, then
 * wakes the page so its own session resumes.
 */
import type {ScheduleTimeout} from '@vibesys/backend-client';
import {
  BaseWindow,
  dialog,
  ipcMain,
  type MessagePortMain,
  nativeTheme,
  net,
  powerMonitor,
  type WebContentsView,
} from 'electron';
import {APP_ENTRY_URL, isAppPage} from './app-assets.js';
import {sandboxedView} from './app-security.js';
import {type AttachedRun, checkAttachment, observedDial} from './attachment.js';
import {CONNECT_CHANNEL, WAKE_CHANNEL} from './bridge-protocol.js';
import type {HostPool} from './host-pool.js';
import {type HostId, type HostKey, hostFromKey, hostKey, hostLabel} from './host-settings.js';
import {type InstanceRecord, versionSkewMessage} from './instances.js';
import type {LaunchPlan} from './launch-args.js';
import {
  launchedRunId,
  learnedRunId,
  type RecentFile,
  type RecentRun,
  type RunLaunch,
  recentStatus,
} from './recent.js';
import {type RelayPort, relay} from './relay.js';
import {type ResumeRunId, resumeArguments} from './resume-run.js';
import {StopController} from './stop-controller.js';
import {suggestCheckout, suggestProjects} from './suggestions.js';
import {type ConnectionStatus, ConnectionSupervisor, type StreamEnd} from './supervisor.js';
import {bindWelcomeIpc, type WelcomeRequest} from './welcome-ipc.js';
import {
  type ChromeState,
  type StartOutcome,
  WELCOME_EVENTS,
  type WelcomeHost,
  type WelcomeOverview,
  type WelcomeRecent,
  type WelcomeRecentStatus,
} from './welcome-protocol.js';
import {TITLEBAR_HEIGHT, windowChrome} from './window-chrome.js';

const WELCOME_URL = 'app://vibesys/welcome.html';
/** How often the main process looks for the network coming back. */
const NETWORK_POLL_MS = 5_000;

export interface DesktopAppDeps {
  readonly pool: HostPool;
  readonly scheduleTimeout: ScheduleTimeout;
  readonly log: (message: string) => void;
}

/** The recent runs as the welcome view lists them. */
function welcomeRecent(recent: RecentFile): WelcomeRecent[] {
  return recent.runs.map(run => ({...run, hostLabel: hostLabel(hostFromKey(run.host))}));
}

function relayPort(port: MessagePortMain): RelayPort {
  return {
    post: message => port.postMessage(message),
    onMessage: listener => port.on('message', event => listener(event.data)),
    onClose: listener => port.on('close', listener),
    close: () => port.close(),
  };
}

interface RunBinding {
  readonly view: WebContentsView;
  readonly run: AttachedRun;
  readonly supervisor: ConnectionSupervisor;
  readonly project: string;
  readonly hostId: HostId;
  /** Null until the run's server reports it (`#observe`); a new run is attached before then. */
  runId: string | null;
  readonly task: string | null;
  connection: ConnectionStatus;
}

export class DesktopApp {
  readonly #deps: DesktopAppDeps;
  readonly #window: BaseWindow;
  readonly #welcome: WebContentsView;
  readonly #stops: StopController;
  #run: RunBinding | null = null;
  #mode: ChromeState['mode'] = 'welcome';
  #online = true;
  /** The host whose panel the welcome view shows first. */
  #firstHost: HostKey = 'local';
  /** Starts the welcome view asked for, by its start token; true once cancelled. */
  readonly #starts = new Map<number, {cancelled: boolean}>();

  constructor(deps: DesktopAppDeps) {
    this.#deps = deps;
    this.#stops = new StopController({
      stop: async (host, instanceId, force) =>
        deps.pool.stopRun(hostFromKey(host), instanceId, force),
      confirm: text => this.#confirmStop(text),
      changed: () => this.#sendChrome(),
    });
    this.#window = new BaseWindow({
      width: 1440,
      height: 900,
      minWidth: 960,
      minHeight: 600,
      title: 'VibeSys',
      ...windowChrome(process.platform),
    });
    this.#welcome = sandboxedView('welcome-preload.cjs');
    this.#window.contentView.addChildView(this.#welcome);
    this.#window.on('resize', () => this.#layout());
    this.#window.on('closed', () => this.#detach());
    this.#layout();
    this.#welcome.webContents
      .loadURL(WELCOME_URL)
      .catch(() => deps.log('could not load the welcome view'));
  }

  /** Answer the welcome view, relay the run view's connections, and watch sleep and network. */
  bind(): void {
    bindWelcomeIpc(ipcMain, this.#welcome.webContents, WELCOME_URL, request =>
      this.#answer(request),
    );
    ipcMain.on(CONNECT_CHANNEL, event => {
      const [port] = event.ports;
      if (port !== undefined) this.#connect(port, event.sender.id, event.senderFrame?.url ?? '');
    });
    // A machine that slept has dead connections nobody has noticed yet: check the link now.
    powerMonitor.on('resume', () => this.#run?.supervisor.dispatch({type: 'resumed'}));
    this.#watchNetwork();
  }

  /** Open what the launch asked for. */
  async open(plan: LaunchPlan): Promise<void> {
    switch (plan.kind) {
      case 'welcome':
        this.#showWelcome(plan.host);
        return;
      case 'instance':
        await this.#attachInstance(plan.host, plan.instanceId, null);
        return;
      case 'start':
        this.#deps.log('starting a detached VibeSys run');
        await this.#startRun({kind: 'local'}, plan.project, null, plan.runArgs);
        return;
      case 'attach':
        await this.#openRun({kind: 'local'}, plan.socketPath, null, '', null, null);
        return;
    }
  }

  /** Bring the window to the front (a second launch of the app asked for it). */
  focus(): void {
    if (this.#window.isDestroyed()) return;
    if (this.#window.isMinimized()) this.#window.restore();
    this.#window.show();
    this.#window.focus();
  }

  /** Tell the welcome view the recent runs changed (the settings store wrote them). */
  recentChanged(recent: RecentFile): void {
    this.#send(WELCOME_EVENTS.recent, welcomeRecent(recent));
  }

  async #confirmStop(text: string): Promise<boolean> {
    const {response} = await dialog.showMessageBox(this.#window, {
      type: 'warning',
      message: text,
      buttons: ['Cancel', 'Stop'],
      defaultId: 0,
      cancelId: 0,
    });
    return response === 1;
  }

  #showWelcome(host: HostId | null): void {
    if (host !== null) {
      this.#firstHost = hostKey(host);
      this.#send(WELCOME_EVENTS.showHost, hostKey(host));
    }
    this.#setMode('welcome');
  }

  #setMode(mode: ChromeState['mode']): void {
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

  #chrome(): ChromeState {
    const run = this.#run;
    return {
      mode: this.#mode,
      stops: this.#stops.views(),
      attached:
        run === null
          ? null
          : {
              host: hostKey(run.hostId),
              hostLabel: run.run.hostName,
              instanceId: run.run.instanceId,
              runId: run.runId,
              project: run.project,
              connection: run.connection,
            },
    };
  }

  #sendChrome(): void {
    this.#send(WELCOME_EVENTS.chrome, this.#chrome());
  }

  #send(channel: string, value: unknown): void {
    if (!this.#welcome.webContents.isDestroyed()) this.#welcome.webContents.send(channel, value);
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
  async #openRun(
    id: HostId,
    socketPath: string,
    instanceId: string | null,
    project: string,
    runId: string | null,
    task: string | null,
  ): Promise<void> {
    const pool = this.#deps.pool;
    const hostName = hostLabel(id);
    const run: AttachedRun = {
      host: await pool.host(id),
      hostName,
      vibesysCommand: await pool.command(id),
      instanceId,
      endpoint: {socketPath},
    };
    this.#detach();
    const runView = sandboxedView('preload.cjs');
    const supervisor: ConnectionSupervisor = new ConnectionSupervisor({
      check: interactive => checkAttachment(run, interactive),
      scheduleTimeout: this.#deps.scheduleTimeout,
      wakePage: () => {
        if (!runView.webContents.isDestroyed()) runView.webContents.send(WAKE_CHANNEL);
      },
      show: connection => this.#connectionChanged(supervisor, connection),
    });
    this.#run = {
      view: runView,
      run,
      supervisor,
      project,
      hostId: id,
      runId,
      task,
      connection: {kind: 'connecting'},
    };
    this.#window.contentView.addChildView(runView);
    this.#window.setTitle(`VibeSys · ${hostName}`);
    runView.webContents
      .loadURL(APP_ENTRY_URL)
      .catch(() => this.#deps.log('could not load the run view'));
    this.#setMode('run');
    supervisor.dispatch({type: 'start'});
  }

  #connectionChanged(supervisor: ConnectionSupervisor, connection: ConnectionStatus): void {
    const run = this.#run;
    if (run?.supervisor !== supervisor) return;
    const changed = connection.kind !== run.connection.kind;
    run.connection = connection;
    if (connection.kind === 'run-ended' && run.run.instanceId !== null) {
      this.#stops.runEnded(hostKey(run.hostId), run.run.instanceId);
    }
    this.#sendChrome();
    if (changed) this.#deps.log(`${run.run.hostName}: ${connection.kind}`);
  }

  /** The record of `instanceId` on `id`; a version mismatch is refused with its message. */
  async #compatibleRecord(id: HostId, instanceId: string) {
    const records = await this.#deps.pool.records(id);
    const record = records.find(candidate =>
      candidate.kind === 'compatible'
        ? candidate.instance.id === instanceId
        : candidate.id === instanceId,
    );
    if (record === undefined) throw new Error(`no live run ${instanceId} on ${hostLabel(id)}`);
    if (record.kind === 'incompatible') throw await this.#versionSkew(id, record);
    return record.instance;
  }

  async #versionSkew(
    id: HostId,
    record: {readonly protocolVersion: unknown; readonly vibesysVersion: string | null},
  ): Promise<Error> {
    return new Error(
      versionSkewMessage({
        hostName: hostLabel(id),
        vibesysCommand: await this.#deps.pool.command(id),
        protocolVersion: record.protocolVersion,
        vibesysVersion: record.vibesysVersion,
      }),
    );
  }

  /** Attach to registry run `instanceId` on `id`. */
  async #attachInstance(id: HostId, instanceId: string, task: string | null): Promise<void> {
    const instance = await this.#compatibleRecord(id, instanceId);
    await this.#remember(id, instance.projectRoot, task, instance.id, instance.runId);
    await this.#openRun(
      id,
      instance.socketPath,
      instanceId,
      instance.projectRoot,
      instance.runId,
      task,
    );
  }

  /**
   * Start a detached run of `task` on `id` in `project` and attach to it. When `cancel` says the
   * start was cancelled by the time the run's record appears, the run is stopped instead.
   */
  async #startRun(
    id: HostId,
    project: string,
    task: string | null,
    args: readonly string[],
    cancel: {readonly cancelled: boolean} = {cancelled: false},
  ): Promise<StartOutcome> {
    const taskArgs = task === null ? [] : ['--task', task];
    return this.#launch(id, project, task, {kind: 'start'}, [...taskArgs, ...args], cancel);
  }

  /** Resume stopped run `run` of `project` on `id`, detached, and attach. */
  async #resumeRun(id: HostId, project: string, run: ResumeRunId): Promise<void> {
    await this.#launch(id, project, null, {kind: 'resume', run}, resumeArguments(run), {
      cancelled: false,
    });
  }

  async #launch(
    id: HostId,
    project: string,
    task: string | null,
    launch: RunLaunch,
    args: readonly string[],
    cancel: {readonly cancelled: boolean},
  ): Promise<StartOutcome> {
    const host = await this.#deps.pool.host(id);
    const server = await host.startServer(args, {cwd: project});
    if (server.record.kind === 'incompatible') throw await this.#versionSkew(id, server.record);
    const {instance} = server.record;
    if (cancel.cancelled) {
      // Stop only the run this start launched, never one that was already live.
      if (!server.alreadyLive) await this.#stopCancelled(id, instance.id);
      return 'cancelled';
    }
    this.#deps.log(
      server.alreadyLive
        ? `the run is already live as ${instance.id}; attaching to it`
        : `started run ${instance.id}; stop it with: vibesys instances stop ${instance.id}`,
    );
    const runId = launchedRunId(launch, instance.runId);
    await this.#remember(id, instance.projectRoot, task, instance.id, runId);
    const socket = server.endpoint.socketPath;
    await this.#openRun(id, socket, instance.id, instance.projectRoot, runId, task);
    return 'started';
  }

  async #stopCancelled(id: HostId, instanceId: string): Promise<void> {
    this.#deps.log(`the start was cancelled; stopping run ${instanceId}`);
    try {
      await this.#deps.pool.stopRun(id, instanceId, false);
    } catch (error) {
      this.#deps.log(`could not stop cancelled run ${instanceId}: ${(error as Error).message}`);
    }
  }

  async #remember(
    id: HostId,
    project: string,
    task: string | null,
    instanceId: string,
    runId: string | null,
  ): Promise<void> {
    const run = {host: hostKey(id), project, task, instanceId, runId, attachedAt: Date.now()};
    try {
      await this.#deps.pool.remember(run);
    } catch (error) {
      this.#deps.log(`could not remember the run: ${(error as Error).message}`);
    }
  }

  /** The Recent entry of `instanceId` on `id`, if any. */
  async #recentEntry(id: HostId, instanceId: string): Promise<RecentRun | undefined> {
    const runs = (await this.#deps.pool.recent()).runs;
    return runs.find(run => run.host === hostKey(id) && run.instanceId === instanceId);
  }

  /** Relay the run view's connection `port`; refuse every other sender. */
  #connect(port: MessagePortMain, sender: number, url: string): void {
    const binding = this.#run;
    if (binding === null || sender !== binding.view.webContents.id || !isAppPage(url)) {
      this.#deps.log('refused a connection from a view that is not bound to a run');
      port.close();
      return;
    }
    port.start();
    const report = (end: StreamEnd, detail: string): void =>
      binding.supervisor.dispatch({type: 'stream-ended', end, detail});
    void relay(relayPort(port), observedDial(binding.run, report));
  }

  /** Report the network coming back (offline to online) to the supervisor. */
  #watchNetwork(): void {
    const poll = (): void => {
      const online = net.isOnline();
      if (online && !this.#online) this.#run?.supervisor.dispatch({type: 'network-changed'});
      this.#online = online;
      this.#deps.scheduleTimeout(poll, NETWORK_POLL_MS);
    };
    this.#deps.scheduleTimeout(poll, NETWORK_POLL_MS);
  }

  /** Answer one validated welcome request. */
  async #answer(request: WelcomeRequest): Promise<unknown> {
    switch (request.kind) {
      case 'overview':
        return this.#overview();
      case 'recent-status':
        return this.#recentStatus(request.host);
      case 'hosts':
        return this.#deps.pool.aliases();
      case 'host':
        return this.#host(request.host);
      case 'set-checkout':
        return this.#deps.pool.setCheckout(request.host, request.path);
      case 'sign-in':
        await this.#deps.pool.signIn(request.host);
        return null;
      case 'tasks':
        return this.#deps.pool.tasks(request.host, request.project);
      default:
        return this.#act(request);
    }
  }

  /** The welcome view's actions: attach, start, cancel, resume, stop, and switching views. */
  async #act(request: WelcomeRequest): Promise<unknown> {
    switch (request.kind) {
      case 'attach': {
        const known = await this.#recentEntry(request.host, request.instance);
        await this.#attachInstance(request.host, request.instance, known?.task ?? null);
        return null;
      }
      case 'start':
        return this.#startWithToken(request);
      case 'cancel-start': {
        // A start that already ended has nothing left to cancel.
        const cancel = this.#starts.get(request.start);
        if (cancel !== undefined) cancel.cancelled = true;
        return null;
      }
      case 'resume':
        await this.#resumeRun(request.host, request.project, request.run);
        return null;
      case 'stop':
        await this.#stop(request.host, request.instance, request.force);
        return null;
      case 'show-run':
      case 'show-welcome':
        this.#setMode(request.kind === 'show-run' ? 'run' : 'welcome');
        return null;
      case 'retry':
        this.#run?.supervisor.dispatch({type: 'user-retry'});
        return null;
      default:
        throw new Error(`unexpected welcome request ${request.kind}`);
    }
  }

  async #startWithToken(request: Extract<WelcomeRequest, {kind: 'start'}>): Promise<StartOutcome> {
    if (this.#starts.has(request.start)) throw new Error('that start was already asked for');
    const cancel = {cancelled: false};
    this.#starts.set(request.start, cancel);
    try {
      return await this.#startRun(
        request.host,
        request.project,
        request.task,
        request.args,
        cancel,
      );
    } finally {
      this.#starts.delete(request.start);
    }
  }

  async #stop(id: HostId, instanceId: string, force: boolean): Promise<void> {
    const known = await this.#recentEntry(id, instanceId);
    const attached = this.#run?.run.instanceId === instanceId ? this.#run : null;
    const label = known?.task ?? known?.runId ?? attached?.runId ?? instanceId;
    await this.#stops.request(
      {host: hostKey(id), hostLabel: hostLabel(id), instanceId, label},
      force,
    );
  }

  async #overview(): Promise<WelcomeOverview> {
    const pool = this.#deps.pool;
    return {
      settingsProblem: await pool.settingsCheck(),
      recent: welcomeRecent(await pool.recent()),
      chrome: this.#chrome(),
      host: this.#firstHost,
    };
  }

  async #recentStatus(id: HostId): Promise<Record<string, WelcomeRecentStatus>> {
    const pool = this.#deps.pool;
    const runs = (await pool.recent()).runs.filter(run => run.host === hostKey(id));
    let records: InstanceRecord[] | {error: string};
    try {
      records = await pool.records(id);
    } catch (error) {
      records = {error: (error as Error).message};
    }
    const statuses: Record<string, WelcomeRecentStatus> = {};
    for (const run of runs) statuses[run.instanceId] = recentStatus(run, records);
    if (Array.isArray(records)) this.#observe(hostKey(id), records);
    return statuses;
  }

  async #host(id: HostId): Promise<WelcomeHost> {
    const pool = this.#deps.pool;
    const checkout = await pool.checkout(id);
    const recent = (await pool.recent()).runs;
    const named = {key: hostKey(id), label: hostLabel(id), checkout};
    if (checkout === null) {
      return {
        ...named,
        suggestedCheckout: await pool.suggestedCheckout(id),
        runs: [],
        projects: suggestProjects([], recent, hostKey(id)),
      };
    }
    const records = await pool.records(id);
    this.#observe(hostKey(id), records);
    return {
      ...named,
      suggestedCheckout: suggestCheckout(records),
      runs: await pool.runs(id, records),
      projects: suggestProjects(records, recent, hostKey(id)),
    };
  }

  /** Tell the stop flows of `host` which runs its registry lists. */
  #observe(host: HostKey, records: readonly InstanceRecord[]): void {
    const run = this.#run;
    if (run !== null && hostKey(run.hostId) === host) {
      const runId = learnedRunId(run.runId, run.run.instanceId, records);
      if (runId !== null && run.run.instanceId !== null) {
        run.runId = runId;
        void this.#remember(run.hostId, run.project, run.task, run.run.instanceId, runId);
        this.#sendChrome();
      }
    }
    const ids = records.map(record =>
      record.kind === 'compatible' ? record.instance.id : (record.id ?? ''),
    );
    this.#stops.observeRegistry(host, new Set(ids));
  }
}
