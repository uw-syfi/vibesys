/**
 * The desktop's composition of its features over the one window: it routes each validated welcome
 * request to the feature that owns it (`hosts`, `run-control`), keeps the window's state
 * (`ChromeState`) in step, and opens what a launch asked for.
 */
import {ipcMain} from 'electron';
import {type HostId, type HostKey, hostFromKey, hostKey, hostLabel} from '../../common/hosts.js';
import {
  type ChromeState,
  WELCOME_EVENTS,
  type WelcomeOverview,
  type WelcomeRecent,
} from '../../common/welcome-protocol.js';
import type {Hosts} from '../features/hosts/index.js';
import type {RunControl} from '../features/run-control/index.js';
import type {RecentFile, SettingsStore} from '../platform/settings/index.js';
import type {LaunchPlan} from './launch-args.js';
import {bindWelcomeIpc, type WelcomeRequest} from './welcome-ipc.js';
import type {AppWindow} from './window.js';
import {WELCOME_URL} from './window.js';

export interface DesktopDeps {
  readonly window: AppWindow;
  readonly hosts: Hosts;
  readonly runs: RunControl;
  readonly settings: SettingsStore;
  readonly log: (message: string) => void;
}

/** The recent runs as the welcome view lists them. */
function welcomeRecent(recent: RecentFile): WelcomeRecent[] {
  return recent.runs.map(run => ({...run, hostLabel: hostLabel(hostFromKey(run.host))}));
}

export class Desktop {
  readonly #deps: DesktopDeps;
  /** The host whose panel the welcome view shows first. */
  #firstHost: HostKey = 'local';

  constructor(deps: DesktopDeps) {
    this.#deps = deps;
    deps.settings.onRecentChanged(recent => {
      deps.window.send(WELCOME_EVENTS.recent, welcomeRecent(recent));
      void this.#watchRecent(recent);
    });
    bindWelcomeIpc(ipcMain, deps.window.welcome.webContents, WELCOME_URL, request =>
      this.#answer(request),
    );
  }

  /** The window's state, for the title strip. */
  chrome(): ChromeState {
    return {
      mode: this.#deps.window.mode,
      attached: this.#deps.runs.attached(),
      stops: this.#deps.runs.stops(),
    };
  }

  /** Bring the window to the front (a second launch of the app asked for it). */
  focus(): void {
    this.#deps.window.focus();
  }

  /** Send the window's state to the welcome view. */
  chromeChanged(): void {
    this.#deps.window.send(WELCOME_EVENTS.chrome, this.chrome());
  }

  /** Open what the launch asked for. */
  async open(plan: LaunchPlan): Promise<void> {
    switch (plan.kind) {
      case 'welcome':
        if (plan.host !== null) {
          this.#firstHost = hostKey(plan.host);
          this.#deps.window.send(WELCOME_EVENTS.showHost, this.#firstHost);
        }
        this.#mode('welcome');
        return;
      case 'instance':
        await this.#deps.runs.attach(plan.host, plan.instanceId);
        return;
      case 'start':
        this.#deps.log('starting a detached VibeSys run');
        await this.#deps.runs.start({kind: 'local'}, plan.project, null, plan.runArgs);
        return;
    }
  }

  /** Watch the hosts of the recent runs, so their masters are warm and their statuses pushed. */
  async #watchRecent(recent: RecentFile): Promise<void> {
    for (const host of new Set(recent.runs.map(run => run.host))) {
      const id: HostId = hostFromKey(host);
      try {
        if ((await this.#deps.hosts.checkout(id)) !== null) await this.#deps.hosts.watch(id);
      } catch (error) {
        this.#deps.log(`could not watch ${host}: ${(error as Error).message}`);
      }
    }
  }

  #mode(mode: ChromeState['mode']): void {
    this.#deps.window.setMode(mode);
    this.chromeChanged();
  }

  async #overview(): Promise<WelcomeOverview> {
    const recent = await this.#deps.settings.recent();
    void this.#watchRecent(recent);
    return {
      settingsProblem: await this.#deps.hosts.settingsProblem(),
      recent: welcomeRecent(recent),
      chrome: this.chrome(),
      host: this.#firstHost,
    };
  }

  /** Answer one validated welcome request. */
  async #answer(request: WelcomeRequest): Promise<unknown> {
    const {hosts, runs} = this.#deps;
    switch (request.kind) {
      case 'overview':
        return this.#overview();
      case 'hosts':
        return hosts.aliases();
      case 'host':
        return hosts.host(request.host);
      case 'set-checkout':
        return hosts.setCheckout(request.host, request.path);
      case 'sign-in':
        await hosts.signIn(request.host);
        return null;
      case 'tasks':
        return hosts.tasks(request.host, request.project);
      case 'complete':
        return hosts.complete(request.host, request.directory);
      case 'attach':
        // The recent entry keeps the run's task: `remember` merges by run id.
        await runs.attach(request.host, request.instance);
        return null;
      case 'start':
        return runs.start(request.host, request.project, request.task, request.args, request.start);
      case 'cancel-start':
        runs.cancelStart(request.start);
        return null;
      case 'resume':
        await runs.resume(request.host, request.project, request.run);
        return null;
      case 'stop':
        await runs.stop(request.host, request.instance, request.force);
        return null;
      case 'show-run':
      case 'show-welcome':
        this.#mode(request.kind === 'show-run' ? 'run' : 'welcome');
        return null;
      case 'retry':
        runs.retry();
        return null;
    }
  }
}
