/**
 * A Fake of the welcome preload's bridge for the shell's journey: in-memory hosts, Recent and
 * preferences, answering like the main process and pushing what changes, so the page runs as it
 * does in the app with no Electron and no host. `installFakeBridge` and `installFakeMachines` run
 * in the page before its scripts (`page.addInitScript`, in that order), so each is self-contained;
 * the test drives pushes through `window.fakeHost`.
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
    },
    fakeHost: {
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
