/**
 * A Fake of the welcome preload's bridge for the shell's journey: in-memory hosts, Recent and
 * preferences, answering like the main process and pushing what changes, so the page runs as it
 * does in the app with no Electron and no host. For New run it answers like a host helper: one
 * machine description for every machine, the vetted harnesses, the examples, each known project's
 * tasks, and preflight checks that fail `project_own_repository` for a project inside another
 * repository (any path under `/vibesys/`) with its `copy_out` fix. `installFakeBridge`,
 * `installFakeMachines` and `installFakeNewRun` run in the page before its scripts
 * (`page.addInitScript`, in that order), so each is self-contained; the test drives pushes
 * through `window.fakeHost`.
 */
import type {ExampleList} from '@vibesys/contracts/host-helper';
import type {WelcomeBridge} from '@vibesys/ipc';

/**
 * The bridge methods one Fake installer provides, as a type its object literal is checked against
 * (`satisfies Record<...>`): a method missing from the literal, or one the real bridge does not
 * have, is a compile error. `fixture-world.ts` checks that the installers it runs together
 * provide every method of `WelcomeBridge`, so a method added to the bridge cannot be forgotten
 * by the Fake: the app would fail on it at run time, in a world no test visited.
 */
export type FakeProvides<Method extends keyof WelcomeBridge> = Record<Method, unknown>;

/** What `installFakeBridge` provides: the app's own requests, preferences, and the pushes. */
export type CoreMethods =
  | 'platform'
  | 'overview'
  | 'preferences'
  | 'setAppearance'
  | 'setNotifications'
  | 'archive'
  | 'runDigests'
  | 'projectRuns'
  | 'harnesses'
  | 'onListing'
  | 'onRecent'
  | 'onPreferences'
  | 'onOpenSettings';

/** What `installFakeMachines` provides: the saved machines and the checkout and sign-in requests. */
export type MachinesMethods =
  | 'machines'
  | 'machine'
  | 'inspectCheckout'
  | 'removeMachine'
  | 'setCheckout'
  | 'cancelCheckout'
  | 'signIn'
  | 'complete';

/** What `installFakeNewRun` provides: what a machine offers, the checks, and starting a run. */
export type NewRunMethods =
  | 'describeMachine'
  | 'helperFeatures'
  | 'harnesses'
  | 'examples'
  | 'tasks'
  | 'taskGoal'
  | 'preflight'
  | 'createFromExample'
  | 'copyOut'
  | 'startRun';

/** What `installFakeUsage` provides: what a project's runs spent in a window. */
export type UsageMethods = 'usageWindow';

/** What `installFakeExampleDestination` provides: what an example's destination holds. */
export type ExampleDestinationMethods = 'exampleDestination';

/** What `installFakeMachineSettings` provides: each machine's clusters, clouds and agent logins. */
export type MachineSettingsMethods =
  | 'compute'
  | 'testCluster'
  | 'addCluster'
  | 'removeCluster'
  | 'agentLogins'
  | 'startLogin';

/** What `installFakeWindows` provides. */
export type WindowsMethods = 'openRunWindow' | 'showMainWindow' | 'openSettings' | 'openExternal';

export interface FakeBridgeSeed {
  readonly theme: string;
  /** The window controls' inset the preload sets, as main reports it for the platform. */
  readonly chromeInset: {readonly width: number; readonly height: number};
  readonly appearance: {readonly mode: string; readonly light: string; readonly dark: string};
  readonly listings: readonly unknown[];
  readonly recent: readonly unknown[];
  /** The saved runs the app could not read (`SkippedRecent`), which Recent leaves out. */
  readonly skippedRecent?: readonly unknown[];
  /**
   * What each run's journal says (`runs.list`'s digest), by run id, as the app reads it
   * (`RunDigestEntry`); a run not named is one the host has no record of. Without it the host
   * answers as an older helper would: it does not offer digests.
   */
  readonly digests?: Readonly<Record<string, unknown>>;
  /** Hosts whose VibeSys is too old to offer digests, whatever `digests` holds. */
  readonly digestless?: readonly string[];
  /**
   * What each machine lists of a project's runs (`runs.list`, as `ListedRun`s), by
   * `<host>|<project>`; a project not named is one the machine cannot list.
   */
  readonly projectRuns?: Readonly<Record<string, readonly unknown[]>>;
  /**
   * What each project's helper answers a usage question with (`usage.window`), by `<host>|<project>`:
   * the figures as the app reads them (`UsageWindowEntry`), `'unsupported'` for a helper that
   * predates the question, or `'failed'` for one that cannot answer. A project not named, and a
   * world without `usage`, answer as a helper that predates it.
   */
  readonly usage?: Readonly<Record<string, unknown>>;
  /** The saved machines, as `hosts.json` and This Mac list them. */
  readonly machines?: readonly {host: string; label: string; checkout: string}[];
  /** What each machine's facts say, by host; a machine not named here is ready with `facts`. */
  readonly reports?: Readonly<Record<string, {kind: string; detail?: string; facts?: null}>>;
  /** What `machine.describe` says on a ready machine. */
  readonly facts?: unknown;
  /** What `checkout.inspect` finds at each path (on any machine); other paths hold nothing. */
  readonly checkouts?: Readonly<Record<string, unknown>>;
  /** What `paths.complete` lists for each directory (on a machine with a checkout saved). */
  readonly directories?: Readonly<Record<string, readonly unknown[]>>;
  /**
   * What each machine's helper answers machine Settings with, by host: its compute settings and its
   * agents' logins, each null for a helper that predates it. A machine not named is such a helper.
   */
  readonly machineSettings?: Readonly<
    Record<string, {readonly compute: unknown; readonly logins: readonly unknown[] | null}>
  >;
  /** What a connection test finds by the cluster's ssh host alias (`''` for a login node). */
  readonly clusterTests?: Readonly<Record<string, unknown>>;
  /** What starting a harness's login answers, by provider. */
  readonly loginStarts?: Readonly<Record<string, unknown>>;
  /** What New run's helper calls answer; without it they answer as an older helper would. */
  readonly newRun?: {
    readonly machine: unknown;
    readonly harnesses: readonly unknown[];
    /** The examples as the helper lists them: with their task details only for a helper that has `example_catalogue`. */
    readonly examples: ExampleList;
    /** The tasks of each project the machine has. */
    readonly projects: Readonly<Record<string, readonly string[]>>;
    /** The goal of each task of the user's own projects, by `PROJECT TASK` (a space between). */
    readonly goals?: Readonly<Record<string, unknown>>;
    /**
     * What each example destination holds, by the destination as typed (feature `example_destination`);
     * a destination not named is absent.
     */
    readonly destinations?: Readonly<
      Record<string, {readonly state: string; readonly project: string | null}>
    >;
    /** What the helper agreed to in `hello`; every feature New run uses (catalogue included) when not given. */
    readonly features?: readonly string[];
    /** The roles the helper reports for a run (feature `run_roles`); the multi roles when not given. */
    readonly roles?: readonly string[];
    /** The machines whose helper fails to list the examples. */
    readonly examplesFail?: readonly string[];
  };
}

/** What the fake helper did for New run, for the journey to read. */
/** A host's listing as the page receives it: its live and ended runs. */
interface HostListing {
  readonly kind: 'listed';
  readonly host: string;
  readonly live: readonly Record<string, unknown>[];
  readonly ended: readonly Record<string, unknown>[];
  readonly unreadable: readonly unknown[];
  readonly unverified: readonly unknown[];
}

export interface FakeNewRun {
  readonly preflights: readonly unknown[];
  readonly started: readonly unknown[];
}

export function installFakeBridge(seed: FakeBridgeSeed): void {
  type Listener = (event: {sequence: number; value: unknown}) => void;
  const listeners: Record<string, Listener[]> = {};
  let sequence = 0;
  let preferences = {
    appearance: seed.appearance,
    notifications: {enabled: true},
    archived: [] as {host: string; runId: string}[],
  };
  const listings = new Map<string, unknown>(
    seed.listings.map(listing => [(listing as {host: string}).host, listing]),
  );
  let recent = seed.recent;
  const push = (event: string, value: unknown): void => {
    sequence += 1;
    for (const listener of listeners[event] ?? []) listener({sequence, value});
  };
  const settingsListeners: (() => void)[] = [];
  const ok = (value: unknown) => Promise.resolve({ok: true, value});
  const on = (event: string) => (listener: Listener) => {
    listeners[event] = [...(listeners[event] ?? []), listener];
  };
  const store = (next: typeof preferences) => {
    preferences = next;
    push('preferences', preferences);
    return ok(preferences);
  };
  Object.assign(window, {
    vibesysWelcome: {
      platform: 'darwin',
      overview: () =>
        ok({
          eventCursor: sequence,
          settingsProblem: null,
          recent,
          skippedRecent: seed.skippedRecent ?? [],
          listings: [...listings.values()],
          chrome: {mode: 'welcome', attached: null, stops: {}},
          host: 'local',
        }),
      preferences: () => ok(preferences),
      setAppearance: (appearance: typeof preferences.appearance) =>
        store({...preferences, appearance}),
      setNotifications: (enabled: boolean) => store({...preferences, notifications: {enabled}}),
      archive: (run: {host: string; runId: string}, archived: boolean) => {
        const others = preferences.archived.filter(
          each => each.host !== run.host || each.runId !== run.runId,
        );
        return store({...preferences, archived: archived ? [...others, run] : others});
      },
      runDigests: () => ok({kind: 'unsupported'}),
      projectRuns: () => ok([]),
      // The Run screen names its models from the host's catalogue; a world that offers one (`installFakeNewRun`) replaces this.
      harnesses: () => ok({kind: 'unsupported'}),
      onListing: on('listing'),
      onRecent: on('recent'),
      onPreferences: on('preferences'),
      onOpenSettings: (listener: () => void) => {
        settingsListeners.push(listener);
      },
    } satisfies FakeProvides<CoreMethods>,
    fakeHost: {
      /** The application menu's "Settings…" (its Command-comma accelerator). */
      openSettings: () => {
        for (const listener of settingsListeners) listener();
      },
      listing: (listing: {host: string}) => {
        listings.set(listing.host, listing);
        push('listing', listing);
      },
      /** The host's current listing, as the page last received it. */
      listingOf: (host: string) => listings.get(host),
      recent: (runs: readonly unknown[]) => {
        recent = runs;
        push('recent', runs);
      },
    },
  });
  // As the preload does, the first paint is in the stored theme, once the root element exists.
  const paint = new MutationObserver(() => {
    if (document.documentElement === null) return;
    document.documentElement.setAttribute('data-theme', seed.theme);
    document.documentElement.style.setProperty(
      '--chrome-inset-width',
      `${seed.chromeInset.width}px`,
    );
    document.documentElement.style.setProperty(
      '--chrome-inset-height',
      `${seed.chromeInset.height}px`,
    );
    paint.disconnect();
  });
  paint.observe(document, {childList: true});
}

/**
 * The Fake's guard against request loops, as the last init script: no page asks the bridge for the
 * same thing (a method with the same arguments) more than `limit` times, however long it is open.
 * A request past the bound is recorded in `fakeHost.requestLoops` and never answered, so a loop
 * stops there instead of freezing the page, and `expectNoRequestLoops` fails the test that hit it.
 */
export function installRequestBound(limit: number): void {
  const bridge = (window as unknown as {vibesysWelcome: Record<string, unknown>}).vibesysWelcome;
  const loops: string[] = [];
  const counts = new Map<string, number>();
  for (const [name, method] of Object.entries(bridge)) {
    if (typeof method !== 'function' || name.startsWith('on')) continue;
    bridge[name] = (...args: unknown[]) => {
      const request = `${name} ${JSON.stringify(args)}`;
      const count = (counts.get(request) ?? 0) + 1;
      counts.set(request, count);
      if (count <= limit) return method(...args);
      if (count === limit + 1) loops.push(request);
      return new Promise(() => {});
    };
  }
  Object.assign((window as unknown as {fakeHost: object}).fakeHost, {requestLoops: loops});
}

/**
 * The run digests part of the Fake, as a further init script after `installFakeBridge`: the
 * host's answer to what a run's journal says (`seed.digests`), or that it offers none.
 */
export function installFakeDigests(seed: FakeBridgeSeed): void {
  const digests = seed.digests;
  Object.assign((window as unknown as {vibesysWelcome: object}).vibesysWelcome, {
    runDigests: (host: string, _project: string, runIds: readonly string[]) => {
      if (digests === undefined || seed.digestless?.includes(host) === true)
        return Promise.resolve({ok: true, value: {kind: 'unsupported'}});
      const runs = new Map(runIds.flatMap(id => (id in digests ? [[id, digests[id]]] : [])));
      return Promise.resolve({ok: true, value: {kind: 'digests', runs}});
    },
  });
}

/**
 * The project listings part of the Fake, as a further init script after `installFakeBridge`: each
 * project's runs on each machine (`seed.projectRuns`), or a failure for a project not named.
 */
export function installFakeProjectRuns(seed: FakeBridgeSeed): void {
  type Listed = {runId: string; name: string; task: string | null; createdAt: number};
  type Server = {projectRoot: string; runId: string; startedAt: number};
  const listings = seed.projectRuns;
  // Without a seeded listing, a machine lists what its servers hold and what the user opened.
  const derived = (host: string, project: string): Listed[] => {
    const served = (seed.listings as {host: string; live?: Server[]; ended?: Server[]}[])
      .filter(listing => listing.host === host)
      .flatMap(listing => [...(listing.live ?? []), ...(listing.ended ?? [])])
      .filter(run => run.projectRoot === project)
      .map((run): [string, Listed] => [
        run.runId,
        {runId: run.runId, name: run.runId, task: null, createdAt: run.startedAt * 1000},
      ]);
    const opened = (seed.recent as (Listed & {host: string; project: string; attachedAt: number})[])
      .filter(each => each.host === host && each.project === project)
      .map((each): [string, Listed] => [
        each.runId,
        {
          runId: each.runId,
          name: each.task ?? each.runId,
          task: each.task,
          createdAt: each.attachedAt,
        },
      ]);
    return [...new Map([...served, ...opened]).values()];
  };
  Object.assign((window as unknown as {vibesysWelcome: object}).vibesysWelcome, {
    projectRuns: (host: string, project: string) => {
      const seeded =
        listings === undefined ? derived(host, project) : listings[`${host}|${project}`];
      // A seeded run without a name of its own is named by its task, else its id.
      const runs = seeded?.map(run => {
        const each = run as Partial<Listed> & {runId: string; task?: string | null};
        return {...each, name: each.name ?? each.task ?? each.runId};
      });
      return Promise.resolve(
        runs === undefined
          ? {ok: false, error: 'Could not list the runs of the project', authNeeded: false}
          : {ok: true, value: runs},
      );
    },
  });
}

/**
 * The usage part of the Fake, as a further init script after `installFakeBridge`: each project's
 * answer to what its runs spent (`seed.usage`), and the questions asked, in `fakeHost.usageAsked`.
 */
export function installFakeUsage(seed: FakeBridgeSeed): void {
  const answers = seed.usage ?? {};
  const asked: unknown[] = [];
  Object.assign((window as unknown as {fakeHost: object}).fakeHost, {usageAsked: asked});
  Object.assign((window as unknown as {vibesysWelcome: object}).vibesysWelcome, {
    usageWindow: (
      host: string,
      project: string,
      runIds: readonly string[],
      since: number,
      until: number,
    ) => {
      asked.push({host, project, runIds, since, until});
      const answer = answers[`${host}|${project}`] ?? 'unsupported';
      if (answer === 'failed') {
        return Promise.resolve({ok: false, error: 'Could not read usage', authNeeded: false});
      }
      return Promise.resolve({
        ok: true,
        value: answer === 'unsupported' ? {kind: 'unsupported'} : {kind: 'usage', usage: answer},
      });
    },
  } satisfies FakeProvides<UsageMethods>);
}

/**
 * The machines part of the Fake, as the main process keeps them: a host plus its checkout, its
 * status from its facts. It runs after `installFakeBridge` (a second init script, also
 * self-contained) and adds the machines requests and the hosts requests they use to its bridge.
 */
export function installFakeMachines(seed: FakeBridgeSeed): void {
  const ok = (value: unknown) => Promise.resolve({ok: true, value});
  let machines = [...(seed.machines ?? [])];
  const reports = new Map(Object.entries(seed.reports ?? {}));
  const checkouts = seed.checkouts ?? {};
  const failed = (error: string) => Promise.resolve({ok: false, error, authNeeded: false});
  const label = (host: string) => (host === 'local' ? 'This Mac' : host.replace(/^ssh:/, ''));
  const report = (host: string) => reports.get(host) ?? {kind: 'ready', facts: seed.facts ?? null};
  const found = (path: string) =>
    (checkouts[path] as {state?: string} | undefined)?.state === 'found';
  Object.assign((window as unknown as {vibesysWelcome: object}).vibesysWelcome, {
    machines: () => ok(machines),
    machine: (host: string) => ok(report(host)),
    inspectCheckout: (_host: string, path: string) => ok(checkouts[path] ?? {state: 'not_found'}),
    removeMachine: (host: string) => {
      machines = machines.filter(machine => machine.host !== host);
      return ok(machines);
    },
    // The hosts feature's checked save: the checkout must hold VibeSys before it is saved.
    setCheckout: (host: string, path: string) => {
      if (!found(path)) return failed(`${path} on ${label(host)} is not a directory.`);
      const others = machines.filter(machine => machine.host !== host);
      // As main lists them: This Mac first, then by name.
      machines = [...others, {host, label: label(host), checkout: path}].sort((left, right) =>
        left.host === 'local'
          ? -1
          : right.host === 'local'
            ? 1
            : left.label.localeCompare(right.label),
      );
      reports.delete(host);
      return ok(path);
    },
    cancelCheckout: () => ok(null),
    signIn: (host: string) => {
      reports.delete(host);
      return ok(null);
    },
    // Completion needs the host's helper, which starts from its saved checkout.
    complete: (host: string, directory: string) => {
      if (!machines.some(machine => machine.host === host)) {
        return failed(`Set the VibeSys checkout for ${label(host)} first.`);
      }
      const entries = seed.directories?.[directory] ?? [];
      return ok({kind: 'listed', directory, entries, truncated: false});
    },
  } satisfies FakeProvides<MachinesMethods>);
}

/** What the fake helper did for machine Settings, for the journey to read. */
export interface FakeMachineSettings {
  /** Each cluster tested, as `host cluster-name`. */
  readonly tested: string[];
  /** Each cluster saved, as `host cluster-name`. */
  readonly added: string[];
  /** Each cluster removed, as `host cluster-name`. */
  readonly removed: string[];
  /** Each login started, as `host provider`. */
  readonly logins: string[];
}

/**
 * The machine Settings half of the Fake, as a further init script: each machine's helper answers
 * what it has set up and how its harnesses are signed in, tests a cluster by its ssh alias, saves
 * one only if it passes the test again, and starts a login as the seed says. What was done is in
 * `fakeHost.machineSettings`.
 */
export function installFakeMachineSettings(seed: FakeBridgeSeed): void {
  const ok = (value: unknown) => Promise.resolve({ok: true, value});
  const refused = (error: string) => Promise.resolve({ok: false, error, authNeeded: false});
  type Cluster = {name: string; reach: {kind: string; host?: string}};
  type Settings = {
    config_path: string;
    clusters: {cluster: Cluster; source: string}[];
    modal: unknown;
  };
  const world = seed.machineSettings ?? {};
  const settings = new Map<string, Settings | null>(
    Object.entries(world).map(([host, each]) => [
      host,
      each.compute === null ? null : (structuredClone(each.compute) as Settings),
    ]),
  );
  const done: FakeMachineSettings = {tested: [], added: [], removed: [], logins: []};
  const label = (host: string) => (host === 'local' ? 'This Mac' : host.replace(/^ssh:/, ''));
  const tests = seed.clusterTests ?? {};
  const test = (cluster: Cluster) =>
    tests[cluster.reach.kind === 'ssh' ? (cluster.reach.host ?? '') : ''] ?? {
      outcome: 'failed',
      stage: 'reach',
      detail: `cannot reach ${cluster.reach.host ?? 'the scheduler'}`,
    };
  const tooOld = (host: string) => refused(`${label(host)} predates machine compute settings.`);
  Object.assign((window as unknown as {vibesysWelcome: object}).vibesysWelcome, {
    compute: (host: string) => {
      const current = settings.get(host);
      return ok(
        current === undefined || current === null
          ? {kind: 'unsupported'}
          : {kind: 'offered', value: current},
      );
    },
    testCluster: (host: string, cluster: Cluster) => {
      done.tested.push(`${host} ${cluster.name}`);
      return settings.get(host) == null ? tooOld(host) : ok(test(cluster));
    },
    addCluster: (host: string, cluster: Cluster) => {
      const current = settings.get(host);
      if (current === undefined || current === null) return tooOld(host);
      if (current.clusters.some(listed => listed.cluster.name === cluster.name)) {
        return refused(`a cluster named '${cluster.name}' is already set up`);
      }
      const result = test(cluster) as {outcome: string; stage?: string; detail?: string};
      if (result.outcome === 'failed') return refused(`${result.stage}: ${result.detail}`);
      current.clusters = [...current.clusters, {cluster, source: 'vibesys'}];
      done.added.push(`${host} ${cluster.name}`);
      return ok(current);
    },
    removeCluster: (host: string, name: string) => {
      const current = settings.get(host);
      if (current === undefined || current === null) return tooOld(host);
      const listed = current.clusters.find(each => each.cluster.name === name);
      if (listed === undefined) return refused(`no cluster named '${name}' is set up`);
      if (listed.source !== 'vibesys')
        return refused(`cluster '${name}' comes from the operator file`);
      current.clusters = current.clusters.filter(each => each !== listed);
      done.removed.push(`${host} ${name}`);
      return ok(current);
    },
    agentLogins: (host: string) => {
      const logins = world[host]?.logins ?? null;
      return ok(logins === null ? {kind: 'unsupported'} : {kind: 'offered', value: logins});
    },
    startLogin: (host: string, provider: string) => {
      done.logins.push(`${host} ${provider}`);
      return ok(
        seed.loginStarts?.[provider] ?? {outcome: 'unavailable', reason: 'no_login_command'},
      );
    },
  } satisfies FakeProvides<MachineSettingsMethods>);
  Object.assign((window as unknown as {fakeHost: object}).fakeHost, {machineSettings: done});
}

/**
 * The example destination half of the Fake, a further init script after `installFakeNewRun`: what
 * each destination holds (`newRun.destinations`; absent unless seeded) for a helper that has
 * `example_destination`, and `unsupported` for one that does not. A page that does not ask never
 * needs it: New run asks only a helper that has the feature.
 */
export function installFakeExampleDestination(seed: FakeBridgeSeed): void {
  const catalog = seed.newRun;
  const offered = catalog?.features?.includes('example_destination') === true;
  Object.assign((window as unknown as {vibesysWelcome: object}).vibesysWelcome, {
    exampleDestination: (_host: string, _example: string, destination: string) =>
      Promise.resolve({
        ok: true,
        value: offered
          ? {
              kind: 'offered',
              value: catalog?.destinations?.[destination] ?? {state: 'absent', project: null},
            }
          : {kind: 'unsupported'},
      }),
  } satisfies FakeProvides<ExampleDestinationMethods>);
}

/**
 * The New run half of the Fake, as a second init script after `installFakeBridge`: it adds the
 * helper's New run answers to the bridge, and what they were asked to `fakeHost.newRun`.
 */
export function installFakeNewRun(seed: FakeBridgeSeed): void {
  const ok = (value: unknown) => Promise.resolve({ok: true, value});
  const catalog = seed.newRun;
  const offered = (value: unknown, failed = false) =>
    failed
      ? refused('The helper failed.')
      : ok(catalog === undefined ? {kind: 'unsupported'} : {kind: 'offered', value});
  const projects = new Map(Object.entries(catalog?.projects ?? {}));
  const newRun = {preflights: [] as unknown[], started: [] as unknown[]};
  const refused = (error: string) => Promise.resolve({ok: false, error, authNeeded: false});
  const created = (project: string, tasks: readonly string[]) => {
    projects.set(project, tasks);
    return {project, tasks};
  };
  // Serialized into the page: it cannot name an import. Includes `REQUIRED_HELPER_FEATURES`.
  const features =
    catalog?.features ??
    'run_options preflight examples run_history run_summary run_trace run_roles example_catalogue run_role_agents project_goal chat_ended_runs'.split(
      ' ',
    );
  const roles =
    catalog?.roles ?? 'multi-orchestrator multi-profiler multi-implementer multi-judge'.split(' ');
  const check = (id: string, state: string, detail: string) => ({id, state, detail, fix: null});
  const preflight = (_host: string, inputs: {project: string}) => {
    newRun.preflights.push(inputs);
    const own = inputs.project.includes('/vibesys/')
      ? {
          id: 'project_own_repository',
          state: 'fail',
          detail: `${inputs.project} is inside the Git repository /home/me/vibesys.`,
          fix: {kind: 'copy_out', suggested_path: '/home/me/vibesys-projects/queue-rs-2'},
        }
      : {id: 'project_own_repository', state: 'ok', detail: 'Its own repository.', fix: null};
    return ok({
      kind: 'offered',
      value: {
        checks: [
          check('checkout_found', 'ok', 'VibeSys 0.9.0 at ~/vibesys.'),
          own,
          check('project_clean', 'ok', 'The project has no uncommitted changes.'),
          check('settings_valid', 'ok', 'vibesys.yaml is valid.'),
          check('docker_reachable', 'ok', 'Docker 27.3.1.'),
          check('agent_image', 'warn', 'No agent image was built here yet.'),
          check('evaluator_available', 'ok', 'The task evaluator is available.'),
        ],
        roles: features.includes('run_roles') ? roles : null,
        backend: 'cpu',
      },
    });
  };
  const page = window as unknown as {vibesysWelcome: object; fakeHost: object};
  const fakeHost = page.fakeHost as unknown as {
    listingOf(host: string): HostListing | undefined;
    listing(listing: HostListing): void;
  };
  Object.assign(page.vibesysWelcome, {
    describeMachine: () => offered(catalog?.machine),
    helperFeatures: () => ok(catalog === undefined ? [] : features),
    harnesses: () => ok({kind: 'listed', harnesses: catalog?.harnesses ?? []}),
    examples: (host: string) => offered(catalog?.examples, catalog?.examplesFail?.includes(host)),
    tasks: (_host: string, project: string) => {
      const tasks = projects.get(project);
      return tasks === undefined ? refused(`${project} is not a VibeSys project.`) : ok(tasks);
    },
    taskGoal: (_host: string, project: string, task: string) => {
      if (!features.includes('project_goal')) return ok({kind: 'unsupported'});
      const goal = catalog?.goals?.[`${project} ${task}`];
      return goal === undefined ? refused(`${project} has no task ${task}.`) : offered(goal);
    },
    preflight,
    // As a real host does, a destination that holds something is refused.
    createFromExample: (_host: string, example: string, destination: string) => {
      const held = catalog?.destinations?.[destination]?.state ?? 'absent';
      const tasks = catalog?.examples.examples.find(each => each.name === example)?.tasks ?? [];
      const made = ['absent', 'empty'].includes(held) ? created(destination, tasks) : null;
      return ok(made === null ? {kind: 'taken'} : {kind: 'created', created: made});
    },
    copyOut: (_host: string, source: string, destination: string) =>
      ok(created(destination, projects.get(source) ?? [])),
    // As a real host does, a started run is listed as live from then on.
    startRun: (host: string, inputs: {project: string}) => {
      newRun.started.push(inputs);
      const runId = '20261015-200000';
      const instanceId = '00000000000c';
      const none = {kind: 'listed', host, live: [], ended: [], unreadable: [], unverified: []};
      const current = fakeHost.listingOf(host) ?? (none as HostListing);
      const run = {id: instanceId, projectRoot: inputs.project, runId, status: 'running'};
      // This function runs in the page, where `STARTED_AT` (started-at.ts) is out of reach: the same moment.
      const startedAt = 1_792_000_000;
      fakeHost.listing({
        ...current,
        live: [...current.live, {...run, phase: 'running', paused: null, end: null, startedAt}],
      });
      return ok({runId, instanceId});
    },
  } satisfies FakeProvides<NewRunMethods>);
  Object.assign(page.fakeHost, {newRun});
}

/** What the fake window requests did, for the journey to read. */
export interface FakeWindows {
  /** Each run asked to open in its own window, as `host run`. */
  readonly opened: string[];
  /** How often the main window was asked to come to the front. */
  shownMain: number;
  /** Each address asked to open outside the app. */
  readonly external: string[];
}

/**
 * The windows part of the Fake, as a further init script: `openRunWindow` opens the run's page in
 * a new browser window, as main opens an `AppWindow`, or focuses the one already showing it. A
 * journey that wants the new window's page to have the bridge too installs the scripts on its
 * browser context.
 */
export function installFakeWindows(): void {
  const windows = new Map<string, Window>();
  const record: FakeWindows = {opened: [], shownMain: 0, external: []};
  const ok = () => Promise.resolve({ok: true, value: null});
  const page = window as unknown as {vibesysWelcome: object; fakeHost: object};
  Object.assign(page.vibesysWelcome, {
    openRunWindow: (run: {host: string; runId: string}) => {
      const key = `${run.host} ${run.runId}`;
      record.opened.push(key);
      const shown = windows.get(key);
      if (shown !== undefined && !shown.closed) {
        shown.focus();
        return ok();
      }
      const query = `host=${encodeURIComponent(run.host)}&run=${encodeURIComponent(run.runId)}`;
      const opened = window.open(`./shell.html?${query}`, '_blank');
      if (opened !== null) windows.set(key, opened);
      return ok();
    },
    showMainWindow: () => {
      record.shownMain += 1;
      return ok();
    },
    openSettings: () => {
      record.shownMain += 1;
      return ok();
    },
    openExternal: (url: string) => {
      record.external.push(url);
      return ok();
    },
  } satisfies FakeProvides<WindowsMethods>);
  Object.assign(page.fakeHost, {windows: record});
}
