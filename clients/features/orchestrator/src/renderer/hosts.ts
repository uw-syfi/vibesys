/**
 * The hook that runs one machine per connected host and hands the screens their views. A host
 * connection is a name and the ports to reach it; the screens never learn whether those ports
 * are a fixture or a remote daemon.
 */
import type {HostPorts} from '@vibesys/orchestrator-source';
import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {createHostActor, type HostActor, type HostView, hostStore} from '../state/index.js';

export interface HostConnection {
  readonly host: string;
  readonly ports: HostPorts;
}

export interface Hosts {
  readonly actors: ReadonlyMap<string, HostActor>;
  readonly views: readonly HostView[];
}

const sameViews = (a: readonly HostView[], b: readonly HostView[]): boolean =>
  a.length === b.length && a.every((view, index) => view === b[index]);

export function useHosts(connections: readonly HostConnection[]): Hosts {
  const [actors, setActors] = useState<readonly HostActor[]>([]);
  useEffect(() => {
    const created = connections.map(({host, ports}) => createHostActor({host, ports}));
    for (const actor of created) actor.start();
    setActors(created);
    return () => {
      for (const actor of created) actor.stop();
    };
  }, [connections]);
  const stores = useMemo(() => actors.map(hostStore), [actors]);
  const last = useRef<readonly HostView[]>([]);
  const subscribe = useCallback(
    (listener: () => void) => {
      const stops = stores.map(store => store.subscribe(listener));
      return () => {
        for (const stop of stops) stop();
      };
    },
    [stores],
  );
  const [, tick] = useState(0);
  useEffect(() => subscribe(() => tick(n => n + 1)), [subscribe]);
  const next = stores.map(store => store.getState());
  if (!sameViews(next, last.current)) last.current = next;
  return {
    actors: new Map(actors.map(actor => [actor.getSnapshot().context.host, actor])),
    views: last.current,
  };
}
