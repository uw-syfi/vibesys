/**
 * The hook that runs one machine per connected host and hands the screens their views. A host
 * connection is an identity and the ports to reach it; the screens never learn whether those
 * ports are a fixture or a remote host. Leaving the window stops every machine, which closes
 * every watch, and then the ports, which release what carries them.
 */
import {type HostId, type HostKey, hostKey} from '@vibesys/hosts';
import type {MachineClock} from '@vibesys/machine';
import type {HostPorts} from '@vibesys/orchestrator-source';
import {useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {
  createHostActor,
  type HostActor,
  type HostView,
  hostStore,
  type NewBackoff,
} from '../state/index.js';

export interface HostConnection {
  readonly host: HostId;
  readonly ports: HostPorts;
}

export interface Hosts {
  readonly actors: ReadonlyMap<HostKey, HostActor>;
  readonly views: readonly HostView[];
}

const sameViews = (a: readonly HostView[], b: readonly HostView[]): boolean =>
  a.length === b.length && a.every((view, index) => view === b[index]);

export function useHosts(
  connections: readonly HostConnection[],
  backoff: NewBackoff,
  clock?: MachineClock,
): Hosts {
  const [actors, setActors] = useState<readonly HostActor[]>([]);
  useEffect(() => {
    const created = connections.map(({host, ports}) =>
      createHostActor({host, ports, backoff}, clock),
    );
    for (const actor of created) actor.start();
    setActors(created);
    return () => {
      for (const actor of created) actor.stop();
      for (const {ports} of connections) ports.close();
    };
  }, [connections, backoff, clock]);
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
    actors: new Map(actors.map(actor => [hostKey(actor.getSnapshot().context.host), actor])),
    views: last.current,
  };
}
