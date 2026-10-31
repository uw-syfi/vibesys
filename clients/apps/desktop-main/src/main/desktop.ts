/**
 * The desktop's composition of its features over its windows: it routes each validated surface
 * request to the feature that owns it (`hosts`, `machines`, `new-run`, `run-control`, `settings`),
 * opens the run windows, and opens what a launch asked for.
 */

import type {Hosts} from '@vibesys/feature-hosts/main';
import type {MachinesMain} from '@vibesys/feature-machines/main';
import type {NewRunMain} from '@vibesys/feature-new-run/main';
import type {RunControl} from '@vibesys/feature-run-control/main';
import type {PreferencesMain} from '@vibesys/feature-settings/main';
import {hostFromKey, hostKey, hostLabel} from '@vibesys/hosts';
import {
  runWindowUrl,
  WELCOME_EVENTS,
  type WelcomeOverview,
  type WelcomeRequest,
} from '@vibesys/ipc';
import type {RunHistoryStore} from '@vibesys/run-history';
import type {RecentRun, RunRef} from '@vibesys/runs';
import type {RecentFile, SettingsStore} from '@vibesys/settings';
import type {LaunchPlan} from './launch-args.js';
import type {RunWindows} from './run-windows.js';
import type {WelcomeFeed} from './welcome-feed.js';
import {
  type WelcomeBinding,
  type WelcomeDocument,
  type WelcomeInvokeEvent,
  type WelcomeIpc,
  WelcomeRouter,
} from './welcome-ipc.js';
import type {AppWindow} from './window.js';

export interface DesktopDeps {
  readonly window: Pick<
    AppWindow,
    'focus' | 'openSettings' | 'releaseSurfaceRun' | 'sendWelcome' | 'welcome'
  >;
  readonly welcome: Pick<WelcomeFeed, 'recent' | 'snapshot' | 'hostSettings'>;
  readonly hosts: Pick<
    Hosts,
    | 'cancelCheckout'
    | 'cancelCheckoutOwner'
    | 'configured'
    | 'complete'
    | 'harnesses'
    | 'readSummary'
    | 'readSpan'
    | 'runDigests'
    | 'projectRuns'
    | 'usageWindow'
    | 'logs'
    | 'recent'
    | 'releaseLogs'
    | 'retireOwner'
    | 'retryListing'
    | 'setCheckout'
    | 'signIn'
    | 'tasks'
  >;
  /** The ended runs' recorded events every window reads from (one copy, one read of the host). */
  readonly history: Pick<RunHistoryStore, 'read'>;
  readonly machines: Pick<
    MachinesMain,
    | 'machines'
    | 'report'
    | 'inspect'
    | 'remove'
    | 'compute'
    | 'testCluster'
    | 'addCluster'
    | 'removeCluster'
    | 'agentLogins'
    | 'startLogin'
  >;
  readonly runs: Pick<
    RunControl,
    'attach' | 'attachReader' | 'attached' | 'detach' | 'resume' | 'start'
  >;
  readonly settings: Pick<SettingsStore, 'onRecentChanged' | 'recentSnapshot' | 'onHostsChanged'>;
  readonly preferences: Pick<PreferencesMain, 'answer'>;
  readonly newRun: Pick<NewRunMain, 'answer'>;
  /** The app shell's page: the only one admitted to the main window's requests. */
  readonly shellUrl: string;
  /** The windows that each show one run, whose surface is the shell's page with the run named. */
  readonly runWindows: Pick<RunWindows, 'open' | 'sendWelcome'>;
  readonly welcomeIpc: WelcomeIpc;
  /** Hand a validated web or mail address to the user's own browser or mail app. */
  openExternal(url: string): Promise<void>;
  readonly log: (message: string) => void;
}

/** The requests of the Machines, machine Settings and New run screens, and of the windows. */
type ScreenRequest = Extract<
  WelcomeRequest,
  {
    readonly kind:
      | 'machines'
      | 'machine'
      | 'inspect-checkout'
      | 'remove-machine'
      | 'compute'
      | 'test-cluster'
      | 'add-cluster'
      | 'remove-cluster'
      | 'agent-logins'
      | 'start-login'
      | 'describe-machine'
      | 'preflight'
      | 'examples'
      | 'create-from-example'
      | 'copy-out'
      | 'start-run'
      | 'helper-features'
      | 'task-goal'
      | 'example-destination'
      | 'open-run-window'
      | 'show-main-window'
      | 'open-settings'
      | 'open-external';
  }
>;

/** The recent runs as the shell lists them. */
function welcomeRecent(recent: RecentFile): RecentRun[] {
  return recent.runs.map(run => ({...run, hostLabel: hostLabel(hostFromKey(run.host))}));
}

export class Desktop {
  readonly #deps: DesktopDeps;
  /** Fences a disk snapshot that overlaps a newer recent-run change event. */
  #recentGeneration = 0;
  /** Every window's surface requests, routed by the window that sent each. */
  readonly #router: WelcomeRouter;

  /** The same current app documents authorized for welcome requests. */
  admitsRenderer(event: WelcomeInvokeEvent): boolean {
    return this.#router.admits(event);
  }

  constructor(deps: DesktopDeps) {
    this.#deps = deps;
    deps.settings.onHostsChanged(() => {
      const event = deps.welcome.hostSettings();
      deps.window.sendWelcome(WELCOME_EVENTS.hostSettings, event);
      deps.runWindows.sendWelcome(WELCOME_EVENTS.hostSettings, event);
    });
    deps.settings.onRecentChanged(recent => {
      const generation = ++this.#recentGeneration;
      const value = welcomeRecent(recent);
      const event = deps.welcome.recent(value);
      deps.window.sendWelcome(WELCOME_EVENTS.recent, event);
      deps.runWindows.sendWelcome(WELCOME_EVENTS.recent, event);
      void this.#watchRecent(recent, generation);
    });
    this.#router = new WelcomeRouter(deps.welcomeIpc);
    this.#router.bind(this.#binding(deps.window.welcome, deps.shellUrl));
  }

  /**
   * How a window's surface is answered: every request through `#answer`, and each replaced
   * document's renderer-owned work retired, keyed by its generation, so windows never retire each
   * other's work.
   */
  #binding(document: WelcomeDocument, url: string): WelcomeBinding {
    const deps = this.#deps;
    return {
      document,
      url,
      answer: (request, rendererGeneration) => this.#answer(request, rendererGeneration),
      retire: async rendererGeneration => {
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
    };
  }

  /** Show `run` in a window of its own, answering that window's surface once it is new. */
  #openRunWindow(run: RunRef): null {
    const title = `VibeSys · ${hostLabel(hostFromKey(run.host))} · ${run.runId}`;
    const {window, created} = this.#deps.runWindows.open(run, title);
    if (created) {
      this.#router.bind(this.#binding(window.welcome, runWindowUrl(this.#deps.shellUrl, run)));
    }
    return null;
  }

  /** Bring the main window to the front (a second launch of the app asked for it). */
  focus(): void {
    this.#deps.window.focus();
  }

  /**
   * Open what the launch asked for: the main window, or the run it named in a window of its own
   * (the run is attached here to learn its identity, then the window attaches it for itself).
   */
  async open(plan: LaunchPlan): Promise<void> {
    const {runs} = this.#deps;
    switch (plan.kind) {
      case 'main-window':
        return;
      case 'instance':
        await runs.attach(plan.host, plan.instanceId);
        break;
      case 'start':
        this.#deps.log('starting a detached VibeSys run');
        await runs.start({kind: 'local'}, plan.project, null, plan.runArgs);
        break;
    }
    const run = runs.attached();
    if (run === null) return;
    runs.detach(run);
    this.#openRunWindow(run);
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

  /** Watch every machine of `hosts.json`, whether or not a recent run names it. */
  async #watchConfigured(): Promise<void> {
    try {
      await this.#deps.hosts.configured();
    } catch (error) {
      this.#deps.log(`could not watch configured hosts: ${(error as Error).message}`);
    }
  }

  async #overview(): Promise<WelcomeOverview> {
    const generation = ++this.#recentGeneration;
    const {recent, skipped} = await this.#deps.settings.recentSnapshot();
    void this.#watchRecent(recent, generation);
    void this.#watchConfigured();
    return this.#deps.welcome.snapshot({recent: welcomeRecent(recent), skippedRecent: skipped});
  }

  /** Answer one validated surface request. */
  async #answer(request: WelcomeRequest, rendererGeneration: string): Promise<unknown> {
    const {hosts, runs} = this.#deps;
    switch (request.kind) {
      case 'overview':
        return this.#overview();
      case 'set-checkout': {
        const root = await hosts.setCheckout(request.host, request.path, {
          owner: rendererGeneration,
          sequence: request.change,
        });
        // The machine the user just saved is configured now.
        void this.#watchConfigured();
        return root;
      }
      case 'cancel-checkout':
        await hosts.cancelCheckout({owner: rendererGeneration, sequence: request.change});
        return null;
      case 'sign-in':
        await hosts.signIn(request.host);
        return null;
      case 'retry-listing':
        hosts.retryListing(request.host);
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
      case 'attach-reader':
        return runs.attachReader(request.host, request.project, request.runId);
      case 'resume':
        await runs.resume(request.host, request.project, request.run);
        return null;
      case 'logs':
        return hosts.logs(request.host, request.instance, request.request, rendererGeneration);
      case 'release-logs':
        hosts.releaseLogs(rendererGeneration);
        return null;
      case 'read-events': {
        const {host, project, runId, from, max} = request;
        return this.#deps.history.read({host: hostKey(host), project, runId}, from, max);
      }
      case 'read-trace': {
        const {host, project, runId, from, max} = request;
        return this.#deps.history.read({host: hostKey(host), project, runId}, from, max, 'trace');
      }
      case 'read-summary':
        return hosts.readSummary(request.host, request.project, request.runId);
      case 'read-span':
        return hosts.readSpan(
          request.host,
          request.project,
          request.runId,
          request.spanId,
          request.from,
          request.max,
        );
      case 'run-digests':
        return hosts.runDigests(request.host, request.project, request.runIds);
      case 'project-runs':
        return hosts.projectRuns(request.host, request.project);
      case 'usage-window':
        return hosts.usageWindow(
          request.host,
          request.project,
          request.runIds,
          request.since,
          request.until,
        );
      case 'release-run':
        this.#deps.window.releaseSurfaceRun();
        return null;
      case 'preferences':
      case 'set-appearance':
      case 'set-notifications':
      case 'archive':
        return this.#deps.preferences.answer(request);
      default:
        // Every other request is a screen's (`ScreenRequest`), which the type checks.
        return this.#screen(request);
    }
  }

  /** Answer one request of the Machines or New run screen, or of the windows. */
  #screen(request: ScreenRequest): Promise<unknown> {
    const {machines} = this.#deps;
    switch (request.kind) {
      case 'machines':
        return machines.machines();
      case 'machine':
        return machines.report(request.host);
      case 'inspect-checkout':
        return machines.inspect(request.host, request.path);
      case 'remove-machine':
        return machines.remove(request.host);
      case 'compute':
        return machines.compute(request.host);
      case 'test-cluster':
        return machines.testCluster(request.host, request.cluster);
      case 'add-cluster':
        return machines.addCluster(request.host, request.cluster);
      case 'remove-cluster':
        return machines.removeCluster(request.host, request.name);
      case 'agent-logins':
        return machines.agentLogins(request.host);
      case 'start-login':
        return machines.startLogin(request.host, request.provider);
      case 'open-run-window':
        return Promise.resolve(this.#openRunWindow(request.run));
      case 'show-main-window':
        this.#deps.window.focus();
        return Promise.resolve(null);
      case 'open-settings':
        this.#deps.window.openSettings();
        this.#deps.window.focus();
        return Promise.resolve(null);
      case 'open-external':
        return this.#deps.openExternal(request.url).then(() => null);
      default:
        return this.#deps.newRun.answer(request);
    }
  }
}
