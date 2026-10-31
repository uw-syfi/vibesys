/**
 * The Run screen's zoom: a view of the window's location (`RunPageAddress`), which holds the route
 * (`routeQuery`), so Back and Forward move it with the rest of the location and the screen keeps
 * no route of its own. Each zoom is a new location; Escape sends `up`.
 */
import {useEffect, useMemo, useSyncExternalStore} from 'react';
import {
  type Route,
  routeFromQuery,
  routeQuery,
  type ZoomEvent,
  zoomStep,
} from '../../state/index.js';

/** The window's location and keys, as the zoom uses them for the run shown. */
export interface RunPageAddress {
  /** The zoom the location holds now, as `routeQuery` writes it. */
  zoom(): string;
  /** Moves the location to `zoom`, which adds a history entry unless it is already there. */
  move(zoom: string): void;
  /** Calls `listener` when the location may have changed; returns a stop. */
  subscribe(listener: () => void): () => void;
  /** Calls `listener` when Escape is pressed outside a text field; returns a stop. */
  onEscape(listener: () => void): () => void;
}

/** The zoom's route and the way to move it. */
export function useZoom(address: RunPageAddress): {
  readonly route: Route;
  readonly zoom: (event: ZoomEvent) => void;
} {
  const query = useSyncExternalStore(address.subscribe, address.zoom, address.zoom);
  const route = useMemo(() => routeFromQuery(query), [query]);
  const zoom = useMemo(
    () => (event: ZoomEvent) =>
      address.move(routeQuery(zoomStep(routeFromQuery(address.zoom()), event))),
    [address],
  );
  useEffect(() => address.onEscape(() => zoom({type: 'up'})), [address, zoom]);
  return {route, zoom};
}
