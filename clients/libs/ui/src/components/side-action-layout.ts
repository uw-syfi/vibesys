import {useSyncExternalStore} from 'react';

/** The width from which a page's primary action sits beside its form; the same `min-width` as `Page`'s `sideAction` CSS. */
export const SIDE_ACTION_MIN_WIDTH = 900;
const SIDE_ACTION_QUERY = `(min-width: ${SIDE_ACTION_MIN_WIDTH}px)`;

function subscribe(listener: () => void): () => void {
  const query = window.matchMedia(SIDE_ACTION_QUERY);
  query.addEventListener('change', listener);
  return () => query.removeEventListener('change', listener);
}

/**
 * Whether the window is wide enough for a `sideAction` page to put its action panel beside the form
 * (true), rather than pinned under it (false). A panel's details can open by default where they
 * have a column of their own, and stay closed where they would cover the form.
 */
export function useSideActionLayout(): boolean {
  return useSyncExternalStore(
    subscribe,
    () => window.matchMedia(SIDE_ACTION_QUERY).matches,
    () => true,
  );
}
