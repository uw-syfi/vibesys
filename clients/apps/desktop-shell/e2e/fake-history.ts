/**
 * A finished run's history served in small pages and held part-way, for the loading screens:
 * `installFakeHistory` runs in the page after `installFakeFixtureServer` and replaces its
 * `readEvents`. `pageSize` events a page; after `hold` pages the next read waits until
 * `fakeHost.releaseHistory()`; `total` says how many events the history holds in all. It is
 * self-contained because it runs in the page.
 */

export interface FakeHistorySeed {
  readonly events: readonly {readonly sequence?: number}[];
  readonly pageSize: number;
  readonly hold: number;
  readonly total: boolean;
}

export function installFakeHistory(seed: FakeHistorySeed): void {
  let served = 0;
  let open: () => void = () => undefined;
  const gate = new Promise<void>(resolve => {
    open = resolve;
  });
  const last = seed.events.at(-1)?.sequence ?? 0;
  const readEvents = async (
    _host: string,
    _project: string,
    _run: string,
    from: number,
    max: number,
  ) => {
    if (served++ >= seed.hold) await gate;
    const page = seed.events
      .filter(event => (event.sequence ?? 0) >= from)
      .slice(0, Math.min(max, seed.pageSize));
    const next = (page.at(-1)?.sequence ?? from - 1) + 1;
    const known = seed.total ? {totalEvents: seed.events.length} : {};
    return {ok: true, value: {kind: 'page', events: page, next, complete: next > last, ...known}};
  };
  const target = window as unknown as {vibesysWelcome: object; fakeHost: object};
  Object.assign(target.vibesysWelcome, {readEvents, readTrace: readEvents});
  Object.assign(target.fakeHost, {releaseHistory: () => open()});
}
