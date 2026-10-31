/**
 * The app shell's entry: the window, over the preload's bridge (`window.vibesysWelcome`).
 */
import '@vibesys/ui/tokens.css';
import '@vibesys/ui/fonts.css';
import '@vibesys/ui/components.css';
import {browserDraftMemory, browserMoreOptionsMemory} from '@vibesys/feature-new-run/renderer';
import {pageAppearance} from '@vibesys/feature-settings/renderer';
import {runOfWindow, type WelcomeBridge} from '@vibesys/ipc';
import {Card, EmptyState, LinkOpener, Page} from '@vibesys/ui';
import {StrictMode} from 'react';
import {createRoot} from 'react-dom/client';
import {followMouseButtons, windowAddress} from './address.js';
import {ProblemHost} from './problem-host.js';
import {shellSessionOptions} from './run-session.js';
import {createShellStores, RunWindow, Shell} from './shell.js';

declare global {
  interface Window {
    vibesysWelcome?: WelcomeBridge;
    vibesysGateway?: import('@vibesys/ipc').GatewayPortBridge;
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
  // A run window's page names its run in its query; the main window's has none.
  const run = runOfWindow(window.location.search);
  followMouseButtons(window);
  const stores = createShellStores(
    bridge,
    pageAppearance(window),
    document.documentElement,
    schedule,
    {
      messages: window,
      address: windowAddress(window, run),
      now: () => Date.now(),
      session: run => shellSessionOptions(run, {document, window, navigator}),
    },
    {
      moreOptions: browserMoreOptionsMemory(() => window.localStorage),
      // A window's own storage: another window starts its own New run.
      draft: browserDraftMemory(() => window.sessionStorage),
    },
  );
  if (run === null) {
    bridge.onOpenSettings(() =>
      stores.sidebar.store.dispatch({type: 'select', screen: {kind: 'settings'}}),
    );
  }
  // A link in written text opens in the user's own browser or mail app, never in this window.
  const openExternal = (url: string): void => void bridge.openExternal(url);
  createRoot(root).render(
    <StrictMode>
      <LinkOpener open={openExternal}>
        <ProblemHost board={stores.problems}>
          {run === null ? (
            <Shell stores={stores} version={String(import.meta.env['VITE_APP_VERSION'])} />
          ) : (
            <RunWindow stores={stores} run={run} />
          )}
        </ProblemHost>
      </LinkOpener>
    </StrictMode>,
  );
}
