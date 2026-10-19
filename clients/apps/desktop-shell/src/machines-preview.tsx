/**
 * The Machines page on its own, over the preload's bridge: the screens checks open it with a
 * fixture world to look at the page before the shell routes to it. Show runs and Settings only
 * record which machine they were pressed for, in `window.machinesPreview`.
 */
import '@vibesys/ui/tokens.css';
import '@vibesys/ui/fonts.css';
import '@vibesys/ui/components.css';
import {createMachinesStore, MachinesPage} from '@vibesys/feature-machines/renderer';
import type {HostKey} from '@vibesys/hosts';
import type {WelcomeBridge} from '@vibesys/ipc';
import {StrictMode, useSyncExternalStore} from 'react';
import {createRoot} from 'react-dom/client';

declare global {
  interface Window {
    vibesysWelcome?: WelcomeBridge;
    machinesPreview?: {shown: HostKey[]; settings: HostKey[]};
  }
}

const root = document.getElementById('root');
const bridge = window.vibesysWelcome;
if (root === null || bridge === undefined) throw new Error('open this inside the app or its Fake');

/** The runs on each machine, from the query: `?runs=local:2,ssh:host-a:1`. */
function runCounts(search: string): Map<HostKey, number> {
  const counts = new Map<HostKey, number>();
  for (const entry of (new URLSearchParams(search).get('runs') ?? '').split(',')) {
    const at = entry.lastIndexOf(':');
    if (entry !== '') counts.set(entry.slice(0, at), Number(entry.slice(at + 1)));
  }
  return counts;
}

const store = createMachinesStore(bridge);
const record = {shown: [] as HostKey[], settings: [] as HostKey[]};
window.machinesPreview = record;
const counts = runCounts(window.location.search);

function Preview() {
  const state = useSyncExternalStore(store.subscribe, store.getState, store.getState);
  return (
    <MachinesPage
      state={state}
      dispatch={store.dispatch}
      runCounts={counts}
      showRuns={host => record.shown.push(host)}
      openSettings={host => record.settings.push(host)}
    />
  );
}

createRoot(root).render(
  <StrictMode>
    <Preview />
  </StrictMode>,
);
