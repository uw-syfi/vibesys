/**
 * The Usage page on its own, over the preload's bridge (`window.vibesysWelcome`), for the screen
 * checks: the shell that places it behind the rail composes the same `UsageScreen` and store. It is
 * built with the shell but never shipped (`copy-ui.mjs` leaves it out). A press on a run's row is
 * recorded in `window.openedRuns`, where the checks read it.
 */
import '@vibesys/ui/tokens.css';
import '@vibesys/ui/fonts.css';
import '@vibesys/ui/components.css';
import {createUsageStore, type SpendRunRef, UsageScreen} from '@vibesys/feature-usage/renderer';
import type {UsageScreenState} from '@vibesys/feature-usage/state';
import type {WelcomeBridge} from '@vibesys/ipc';
import {type JSX, StrictMode, useSyncExternalStore} from 'react';
import {createRoot} from 'react-dom/client';

declare global {
  interface Window {
    vibesysWelcome?: WelcomeBridge;
    openedRuns?: SpendRunRef[];
  }
}

const root = document.getElementById('root');
const bridge = window.vibesysWelcome;
if (root === null) throw new Error('usage.html has no #root');
if (bridge === undefined) throw new Error('usage.html runs only over the welcome bridge');

const usage = createUsageStore(bridge, {
  schedule: (callback, delayMs) => {
    const timer = window.setTimeout(callback, delayMs);
    return () => window.clearTimeout(timer);
  },
  clock: () => {
    const now = Date.now();
    return {now, midnight: new Date(now).setHours(0, 0, 0, 0)};
  },
});
window.openedRuns = [];

function Page(): JSX.Element {
  const state: UsageScreenState = useSyncExternalStore(
    usage.store.subscribe,
    usage.store.getState,
    usage.store.getState,
  );
  return (
    <UsageScreen
      state={state}
      actions={usage.actions}
      openRun={run => window.openedRuns?.push(run)}
    />
  );
}

createRoot(root).render(
  <StrictMode>
    <Page />
  </StrictMode>,
);
