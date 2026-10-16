/**
 * A Fake of the welcome preload's bridge for the shell's journey: in-memory hosts, Recent and
 * preferences, answering like the main process and pushing what changes, so the page runs as it
 * does in the app with no Electron and no host. `installFakeBridge` runs in the page before its
 * scripts (`page.addInitScript`), so it is self-contained; the test drives pushes through
 * `window.fakeHost`.
 */

export interface FakeBridgeSeed {
  readonly theme: string;
  /** The window controls' inset the preload sets, as main reports it for the platform. */
  readonly chromeInset: {readonly width: number; readonly height: number};
  readonly appearance: {readonly mode: string; readonly light: string; readonly dark: string};
  readonly listings: readonly unknown[];
  readonly recent: readonly unknown[];
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
