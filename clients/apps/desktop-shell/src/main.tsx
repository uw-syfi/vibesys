/**
 * The app shell's entry: the redesigned window, over the welcome preload's bridge
 * (`window.vibesysWelcome`), shown under `VIBESYS_DESKTOP_SHELL=next`.
 */
import '@vibesys/ui/tokens.css';
import '@vibesys/ui/fonts.css';
import '@vibesys/ui/components.css';
import {pageAppearance} from '@vibesys/feature-settings/renderer';
import {runOfWindow, type WelcomeBridge} from '@vibesys/ipc';
import {Card, EmptyState, Page} from '@vibesys/ui';
import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import {shellSessionOptions} from './run-session.js';
import {createShellStores, RunWindow, Shell} from './shell.js';

declare global {
  interface Window {
    vibesysWelcome?: WelcomeBridge;
  }
}

const root = document.getElementById('root');
if (root === null) throw new Error('shell.html has no #root');
const bridge = window.vibesysWelcome;
if (bridge === undefined) {
  createRoot(root).render(
    <Page>
      <Card>
        <EmptyState
          title="Open this in the VibeSys app"
          body="This page runs inside the desktop app."
        />
      </Card>
    </Page>,
  );
} else {
  const schedule = (callback: () => void, delayMs: number): (() => void) => {
    const timer = window.setTimeout(callback, delayMs);
    return () => window.clearTimeout(timer);
  };
  const stores = createShellStores(
    bridge,
    pageAppearance(window),
    document.documentElement,
    schedule,
    {
      messages: window,
      now: () => Date.now(),
      session: run => shellSessionOptions(run, {document, window, navigator}),
    },
  );
  // A run window's page names its run in its query; the main window's has none.
  const run = runOfWindow(window.location.search);
  createRoot(root).render(
    <StrictMode>
      {run === null ? (
        <Shell stores={stores} version={String(import.meta.env['VITE_APP_VERSION'])} />
      ) : (
        <RunWindow stores={stores} run={run} />
      )}
    </StrictMode>,
  );
}
