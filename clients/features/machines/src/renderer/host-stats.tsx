/**
 * A host's live stats, composed: the subscription's effects handed to the card that draws them.
 * The page takes it as a slot, because presentational code reaches no effects.
 */
import type {HostKey} from '@vibesys/hosts';
import {type JSX, useMemo, useSyncExternalStore} from 'react';
import type {HostLive, Machine} from '../state/index.js';
import {type LiveStats, useHostStats} from './effects/live.js';

/** What `render` draws for `host`, given the host's live stats. */
export function WithHostLive(props: {
  readonly live: LiveStats;
  readonly host: HostKey;
  readonly render: (live: HostLive) => JSX.Element;
}): JSX.Element {
  return props.render(useHostStats(props.live, props.host));
}

/**
 * The hosts whose stream delivers right now, read while `watching`. It reads the sources the cards
 * read (the hub shares one stream per host), so the rail and the cards fold the one fact; nothing
 * is read, and no stream is held, while not watching.
 */
export function useDeliveringHosts(
  live: LiveStats | null,
  machines: readonly Machine[],
  watching: boolean,
): ReadonlySet<HostKey> {
  // The sources follow the hosts, not the machines' other facts, which change all the time.
  const hosts = machines.map(each => each.host).join('\n');
  const sources = useMemo(
    () =>
      live === null || !watching || hosts === ''
        ? []
        : hosts.split('\n').map(host => [host, live.source(host)] as const),
    [live, hosts, watching],
  );
  const subscribe = useMemo(
    () => (listener: () => void) => {
      const releases = sources.map(([, source]) => source.subscribe(listener));
      return () => {
        for (const release of releases) release();
      };
    },
    [sources],
  );
  const read = (): string =>
    sources
      .filter(([, source]) => source.getSnapshot().kind === 'live')
      .map(([host]) => host)
      .join('\n');
  const delivering = useSyncExternalStore(subscribe, read, read);
  return useMemo(() => new Set(delivering === '' ? [] : delivering.split('\n')), [delivering]);
}
