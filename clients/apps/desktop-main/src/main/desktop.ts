/**
 * The desktop's composition of its features over the one window: it routes each validated welcome
 * request to the feature that owns it (`hosts`, `run-control`), keeps the window's state
 * (`ChromeState`) in step, and opens what a launch asked for.
 */

import type {Hosts} from '@vibesys/feature-hosts/main';
import type {RunControl} from '@vibesys/feature-run-control/main';
import type {PreferencesMain} from '@vibesys/feature-settings/main';
import {shellWords} from '@vibesys/feature-start-run/state';
import {type HostKey, hostFromKey, hostKey, hostLabel} from '@vibesys/hosts';
import {
  type ChromeState,
  WELCOME_EVENTS,
  type WelcomeOverview,
  type WelcomeRequest,
} from '@vibesys/ipc';
import type {RecentRun} from '@vibesys/runs';
import type {RecentFile, SettingsStore} from '@vibesys/settings';
import type {LaunchPlan} from './launch-args.js';
import type {WelcomeFeed} from './welcome-feed.js';
import {bindWelcomeIpc, type WelcomeIpc} from './welcome-ipc.js';
import type {AppWindow} from './window.js';

export interface DesktopDeps {
  readonly window: Pick<AppWindow, 'focus' | 'mode' | 'sendWelcome' | 'setMode' | 'welcome'>;
  readonly welcome: Pick<WelcomeFeed, 'chrome' | 'recent' | 'showHost' | 'snapshot'>;
  readonly hosts: Pick<
    Hosts,
    | 'aliases'
    | 'cancelCheckout'
    | 'cancelCheckoutOwner'
    | 'cancelInspection'
    | 'cancelInspectionOwner'
    | 'complete'
    | 'harnesses'
    | 'host'
    | 'inspect'
    | 'logs'
    | 'recent'
    | 'releaseLogs'
    | 'retireInspectionOwner'
    | 'retireOwner'
    | 'setCheckout'
    | 'settingsProblem'
    | 'signIn'
    | 'tasks'
  >;
  readonly runs: Pick<
    RunControl,
    | 'attach'
    | 'attached'
    | 'cancelStart'
    | 'resume'
    | 'retireStartOwner'
    | 'retry'
    | 'start'
    | 'stop'
    | 'stops'
  >;
  readonly settings: Pick<SettingsStore, 'onRecentChanged' | 'recent'>;
  readonly preferences: Pick<PreferencesMain, 'answer'>;
  /** The page the window's trusted surface loads, the only one admitted to its requests. */
  readonly surfaceUrl: string;
  readonly welcomeIpc: WelcomeIpc;
  readonly log: (message: string) => void;
}

/** The recent runs as the welcome view lists them. */
function welcomeRecent(recent: RecentFile): RecentRun[] {
  return recent.runs.map(run => ({...run, hostLabel: hostLabel(hostFromKey(run.host))}));
}

export class Desktop {
  readonly #deps: DesktopDeps;
  /** The host whose panel the welcome view shows first. */
  #firstHost: HostKey = 'local';
  /** The document that may still own an inspection write. */
  #welcomeRenderer: string | null = null;
  /** The last Chrome mode published to the welcome renderer. */
  #publishedMode: ChromeState['mode'];
  /** Fences a disk snapshot that overlaps a newer recent-run change event. */
  #recentGeneration = 0;

  constructor(deps: DesktopDeps) {
    this.#deps = deps;
    this.#publishedMode = deps.window.mode;
    deps.settings.onRecentChanged(recent => {
      const generation = ++this.#recentGeneration;
      const value = welcomeRecent(recent);
      deps.window.sendWelcome(WELCOME_EVENTS.recent, deps.welcome.recent(value));
      void this.#watchRecent(recent, generation);
    });
    bindWelcomeIpc(
      deps.welcomeIpc,
      deps.window.welcome,
      deps.surfaceUrl,
      (request, rendererGeneration) => this.#answer(request, rendererGeneration),
      rendererGeneration => {
        // Renderer-owned writes and starts lose authority in the navigation turn itself. A late
        // launch continuation must see cancellation before asynchronous retirement can begin.
        deps.hosts.retireInspectionOwner(rendererGeneration);
        deps.runs.retireStartOwner(rendererGeneration);
        if (this.#welcomeRenderer === rendererGeneration) this.#welcomeRenderer = null;
      },
      async rendererGeneration => {
        const cleanups = [
          {
            name: 'checkout cancellation',
            run: () => deps.hosts.cancelCheckoutOwner(rendererGeneration),
          },
          {name: 'host retirement', run: () => deps.hosts.retireOwner(rendererGeneration)},
        ] as const;
        const settled = await Promise.allSettled(cleanups.map(async cleanup => cleanup.run()));
        const failures = settled.flatMap((result, index) =>
          result.status === 'rejected'
            ? [`${cleanups[index]?.name}: ${String(result.reason)}`]
            : [],
        );
        if (failures.length > 0) {
          deps.log(`could not retire checkout changes: ${failures.join('; ')}`);
        }
      },
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
    const chrome = this.chrome();
    if (this.#publishedMode !== 'run' && chrome.mode === 'run' && this.#welcomeRenderer !== null) {
      this.#deps.hosts.cancelInspectionOwner(this.#welcomeRenderer);
    }
    this.#publishedMode = chrome.mode;
    this.#deps.window.sendWelcome(WELCOME_EVENTS.chrome, this.#deps.welcome.chrome(chrome));
  }

  /** Open what the launch asked for. */
  async open(plan: LaunchPlan): Promise<void> {
    switch (plan.kind) {
      case 'welcome':
        if (plan.host !== null) {
          this.#firstHost = hostKey(plan.host);
          this.#deps.window.sendWelcome(
            WELCOME_EVENTS.showHost,
            this.#deps.welcome.showHost(this.#firstHost),
          );
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

  /** Reconcile the hosts of the bounded recent-run file unless a newer file already arrived. */
  async #watchRecent(recent: RecentFile, generation: number): Promise<void> {
    if (generation !== this.#recentGeneration) return;
    try {
      await this.#deps.hosts.recent([...new Set(recent.runs.map(run => hostFromKey(run.host)))]);
    } catch (error) {
      this.#deps.log(`could not watch recent hosts: ${(error as Error).message}`);
    }
  }

  #mode(mode: ChromeState['mode']): void {
    this.#deps.window.setMode(mode);
    this.chromeChanged();
  }

  async #overview(): Promise<WelcomeOverview> {
    const generation = ++this.#recentGeneration;
    const recent = await this.#deps.settings.recent();
    void this.#watchRecent(recent, generation);
    const settingsProblem = await this.#deps.hosts.settingsProblem();
    return this.#deps.welcome.snapshot({
      settingsProblem,
      recent: welcomeRecent(recent),
      chrome: this.chrome(),
      host: this.#firstHost,
    });
  }

  /** Answer one validated welcome request. */
  async #answer(request: WelcomeRequest, rendererGeneration: string): Promise<unknown> {
    const {hosts, runs} = this.#deps;
    this.#welcomeRenderer = rendererGeneration;
    switch (request.kind) {
      case 'overview':
        return this.#overview();
      case 'hosts':
        return hosts.aliases();
      case 'host':
        return hosts.host(request.host, rendererGeneration);
      case 'set-checkout':
        return hosts.setCheckout(request.host, request.path, {
          owner: rendererGeneration,
          sequence: request.change,
        });
      case 'cancel-checkout':
        await hosts.cancelCheckout({owner: rendererGeneration, sequence: request.change});
        return null;
      case 'cancel-inspection':
        hosts.cancelInspection({owner: rendererGeneration, sequence: request.inspection});
        return null;
      case 'sign-in':
        await hosts.signIn(request.host);
        return null;
      case 'tasks':
        return hosts.tasks(request.host, request.project);
      case 'harnesses':
        return hosts.harnesses(request.host);
      case 'complete':
        return hosts.complete(request.host, request.directory);
      case 'attach':
        // The recent entry keeps the run's task: `remember` merges by run id.
        await runs.attach(request.host, request.instance);
        return null;
      case 'start':
        // The start-run feature owns the run arguments' syntax.
        return runs.start(
          request.host,
          request.project,
          request.task,
          shellWords(request.args),
          {owner: rendererGeneration, sequence: request.start},
          request.harness,
        );
      case 'cancel-start':
        runs.cancelStart({owner: rendererGeneration, sequence: request.start});
        return null;
      case 'resume':
        await runs.resume(request.host, request.project, request.run);
        return null;
      case 'stop':
        await runs.stop(request.host, request.instance, request.force);
        return null;
      case 'inspect':
        return hosts.inspect(request.host, request.instance, {
          owner: rendererGeneration,
          sequence: request.inspection,
        });
      case 'logs':
        return hosts.logs(request.host, request.instance, request.request, rendererGeneration);
      case 'release-logs':
        hosts.releaseLogs(rendererGeneration);
        return null;
      case 'show-run':
      case 'show-welcome':
        this.#mode(request.kind === 'show-run' ? 'run' : 'welcome');
        return null;
      case 'retry':
        runs.retry();
        return null;
      case 'preferences':
      case 'set-appearance':
      case 'set-notifications':
      case 'archive':
        return this.#deps.preferences.answer(request);
    }
  }
}
