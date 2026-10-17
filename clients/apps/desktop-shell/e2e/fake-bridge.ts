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

export interface FakeBridgeSeed {
  readonly theme: string;
  /** The window controls' inset the preload sets, as main reports it for the platform. */
  readonly chromeInset: {readonly width: number; readonly height: number};
  readonly appearance: {readonly mode: string; readonly light: string; readonly dark: string};
  readonly listings: readonly unknown[];
  readonly recent: readonly unknown[];
  /** The saved machines, as `hosts.json` and This Mac list them. */
  readonly machines?: readonly {host: string; label: string; checkout: string}[];
  /** What each machine's facts say, by host; a machine not named here is ready with `facts`. */
  readonly reports?: Readonly<Record<string, {kind: string; detail?: string}>>;
  /** What `machine.describe` says on a ready machine. */
  readonly facts?: unknown;
  /** What `checkout.inspect` finds at each path (on any machine); other paths hold nothing. */
  readonly checkouts?: Readonly<Record<string, unknown>>;
  /** What `paths.complete` lists for each directory (on a machine with a checkout saved). */
  readonly directories?: Readonly<Record<string, readonly unknown[]>>;
  /** What New run's helper calls answer; without it they answer as an older helper would. */
  readonly newRun?: {
    readonly machine: unknown;
    readonly harnesses: readonly unknown[];
    readonly examples: {
      readonly checkout: string;
      readonly examples: readonly {
        readonly name: string;
        readonly category: string;
        readonly tasks: readonly string[];
        readonly external_repo: boolean;
      }[];
    };
    /** The tasks of each project the machine has. */
    readonly projects: Readonly<Record<string, readonly string[]>>;
    /** What the helper agreed to in `hello`; every feature New run uses when not given. */
    readonly features?: readonly string[];
    /** The roles the helper reports for a run (feature `run_roles`); the multi roles when not given. */
    readonly roles?: readonly string[];
  };
}

/** What the fake helper did for New run, for the journey to read. */
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
  const toggleListeners: (() => void)[] = [];
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
      onListing: on('listing'),
      onRecent: on('recent'),
      onPreferences: on('preferences'),
      onChrome: on('chrome'),
      onShowHost: on('showHost'),
      onToggleSidebar: (listener: () => void) => {
        toggleListeners.push(listener);
      },
    },
    fakeHost: {
      /** The application menu's "Toggle Sidebar". */
      toggleSidebar: () => {
        for (const listener of toggleListeners) listener();
      },
      listing: (listing: {host: string}) => {
        listings.set(listing.host, listing);
        push('listing', listing);
      },
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
  });
}

/**
 * The New run half of the Fake, as a second init script after `installFakeBridge`: it adds the
 * helper's New run answers to the bridge, and what they were asked to `fakeHost.newRun`.
 */
export function installFakeNewRun(seed: FakeBridgeSeed): void {
  const ok = (value: unknown) => Promise.resolve({ok: true, value});
  const catalog = seed.newRun;
  const offered = (value: unknown) =>
    ok(catalog === undefined ? {kind: 'unsupported'} : {kind: 'offered', value});
  const projects = new Map(Object.entries(catalog?.projects ?? {}));
  const newRun = {preflights: [] as unknown[], started: [] as unknown[]};
  const refused = (error: string) => Promise.resolve({ok: false, error, authNeeded: false});
  const created = (project: string, tasks: readonly string[]) => {
    projects.set(project, tasks);
    return ok({project, tasks});
  };
  const insideRepository = (project: string) => project.includes('/vibesys/');
  const features = catalog?.features ?? ['run_options', 'preflight', 'examples', 'run_roles'];
  const roles = catalog?.roles ?? [
    'multi-orchestrator',
    'multi-profiler',
    'multi-implementer',
    'multi-judge',
  ];
  const preflight = (_host: string, inputs: {project: string}) => {
    newRun.preflights.push(inputs);
    const own = insideRepository(inputs.project)
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
          {id: 'checkout_found', state: 'ok', detail: 'VibeSys 0.9.0 at ~/vibesys.', fix: null},
          own,
          {id: 'settings_valid', state: 'ok', detail: 'vibesys.yaml is valid.', fix: null},
          {id: 'docker_reachable', state: 'ok', detail: 'Docker 27.3.1.', fix: null},
          {
            id: 'agent_image',
            state: 'warn',
            detail: 'No agent image was built here yet; the first run builds it.',
            fix: null,
          },
        ],
        roles: features.includes('run_roles') ? roles : null,
      },
    });
  };
  const page = window as unknown as {vibesysWelcome: object; fakeHost: object};
  Object.assign(page.vibesysWelcome, {
    describeMachine: () => offered(catalog?.machine),
    helperFeatures: () => ok(catalog === undefined ? [] : features),
    harnesses: () => ok({kind: 'listed', harnesses: catalog?.harnesses ?? []}),
    examples: () => offered(catalog?.examples),
    tasks: (_host: string, project: string) => {
      const tasks = projects.get(project);
      return tasks === undefined ? refused(`${project} is not a VibeSys project.`) : ok(tasks);
    },
    preflight,
    createFromExample: (_host: string, example: string, destination: string) =>
      created(
        destination,
        catalog?.examples.examples.find(each => each.name === example)?.tasks ?? [],
      ),
    copyOut: (_host: string, source: string, destination: string) =>
      created(destination, projects.get(source) ?? []),
    startRun: (_host: string, inputs: unknown) => {
      newRun.started.push(inputs);
      return ok({runId: '20261015-200000', instanceId: '00000000000c'});
    },
  });
  Object.assign(page.fakeHost, {newRun});
}

/** What the fake window requests did, for the journey to read. */
export interface FakeWindows {
  /** Each run asked to open in its own window, as `host run`. */
  readonly opened: string[];
  /** How often the main window was asked to come to the front. */
  shownMain: number;
}

/**
 * The windows part of the Fake, as a further init script: `openRunWindow` opens the run's page in
 * a new browser window, as main opens an `AppWindow`, or focuses the one already showing it. A
 * journey that wants the new window's page to have the bridge too installs the scripts on its
 * browser context.
 */
export function installFakeWindows(): void {
  const windows = new Map<string, Window>();
  const record: FakeWindows = {opened: [], shownMain: 0};
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
  });
  Object.assign(page.fakeHost, {windows: record});
}
