/**
 * The Run screen's zoom for as long as it shows one run: the route machine (`zoomMachine`) run as
 * an actor, whose every zoom is pushed into the window's address (so browser Back walks it back),
 * restored from the address when the window goes Back or Forward, and sent `up` on Escape. The
 * window's address and keys come from the app (`RunPageAddress`), so the screen reads no global.
 */
import {useEffect, useMemo, useState, useSyncExternalStore} from 'react';
import {createActor} from 'xstate';
import {
  type Route,
  RUN_ROUTE,
  routeFromQuery,
  routeQuery,
  type ZoomEvent,
  zoomMachine,
} from '../../state/index.js';

/** The window's address and keys, as the zoom uses them. */
export interface RunPageAddress {
  /** The address's fragment now (without `#`). */
  fragment(): string;
  /** Adds a history entry whose fragment is `fragment`. */
  push(fragment: string): void;
  /** Calls `listener` with the fragment when the window goes Back or Forward; returns a stop. */
  onRestore(listener: (fragment: string) => void): () => void;
  /** Calls `listener` when Escape is pressed outside a text field; returns a stop. */
  onEscape(listener: () => void): () => void;
}

type ZoomActor = ReturnType<typeof startZoom>;

function startZoom(address: RunPageAddress) {
  const actor = createActor(
    zoomMachine.provide({
      actions: {remember: ({context}) => address.push(routeQuery(context.route))},
    }),
  ).start();
  actor.send({type: 'restore', route: routeFromQuery(address.fragment())});
  return actor;
}

const NO_ZOOM = {
  subscribe: () => () => {},
  getRoute: (): Route => RUN_ROUTE,
};

/** The zoom's route and the way to move it, for the run `key`. */
export function useZoom(
  key: string,
  address: RunPageAddress,
): {readonly route: Route; readonly zoom: (event: ZoomEvent) => void} {
  const [actor, setActor] = useState<ZoomActor | null>(null);
  // biome-ignore lint/correctness/useExhaustiveDependencies: the zoom belongs to one run (`key`); the address is the window's for the page's life, and listing it would restart the zoom whenever the caller passes an equal object.
  useEffect(() => {
    const started = startZoom(address);
    const stopRestore = address.onRestore(fragment =>
      started.send({type: 'restore', route: routeFromQuery(fragment)}),
    );
    const stopEscape = address.onEscape(() => started.send({type: 'up'}));
    setActor(started);
    return () => {
      stopRestore();
      stopEscape();
      started.stop();
      setActor(null);
    };
  }, [key]);
  const source = useMemo(
    () =>
      actor === null
        ? NO_ZOOM
        : {
            subscribe: (listener: () => void) => {
              const subscription = actor.subscribe(listener);
              return () => subscription.unsubscribe();
            },
            getRoute: () => actor.getSnapshot().context.route,
          },
    [actor],
  );
  const route = useSyncExternalStore(source.subscribe, source.getRoute, source.getRoute);
  const zoom = useMemo(
    () => (actor === null ? () => undefined : (event: ZoomEvent) => actor.send(event)),
    [actor],
  );
  return {route, zoom};
}
