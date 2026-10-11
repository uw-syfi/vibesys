/**
 * The desktop renderer's entry, the welcome view's composition root: it builds the feature stores
 * over the welcome preload's bridge (`wiring/`), subscribes React to them, and renders the view
 * (`ui/`) with each store's state and dispatcher.
 */

import type {WelcomeBridge} from '@vibesys/ipc';
import type {Store} from '@vibesys/store';
import {type JSX, StrictMode, useSyncExternalStore} from 'react';
import {createRoot} from 'react-dom/client';
import {WelcomeView} from './ui/welcome-view.js';
import {loadWelcomeStores, type WelcomeStores} from './wiring/stores.js';
import '@vibesys/ui/tokens.css';
// Each feature styles its own views; the view's layout is `welcome.css`.
import '@vibesys/feature-hosts/styles.css';
import '@vibesys/feature-run-control/styles.css';
import '@vibesys/feature-start-run/styles.css';
import './welcome.css';

declare global {
  interface Window {
    vibesysWelcome?: WelcomeBridge;
  }
}

function useStore<S, A>(store: Store<S, A>): S {
  return useSyncExternalStore(store.subscribe, store.getState, store.getState);
}

function Root({stores}: {readonly stores: WelcomeStores}): JSX.Element {
  return (
    <WelcomeView
      welcome={useStore(stores.welcome)}
      lists={useStore(stores.lists)}
      host={useStore(stores.host)}
      start={useStore(stores.start)}
      picker={useStore(stores.picker)}
      completion={useStore(stores.completion)}
      send={{
        welcome: stores.welcome.dispatch,
        host: stores.host.dispatch,
        start: stores.start.dispatch,
        picker: stores.picker.dispatch,
        completion: stores.completion.dispatch,
      }}
    />
  );
}

async function load(root: HTMLElement, bridge: WelcomeBridge): Promise<void> {
  document.documentElement.dataset['platform'] = bridge.platform;
  const reactRoot = createRoot(root);
  const loaded = await loadWelcomeStores(bridge, question => window.confirm(question));
  if (!loaded.ok) {
    reactRoot.render(<p className="status error">{loaded.error}</p>);
    return;
  }
  const stores = loaded.value;
  reactRoot.render(
    <StrictMode>
      <Root stores={stores} />
    </StrictMode>,
  );
}

const root = document.getElementById('root');
if (root === null) throw new Error('welcome: #root is missing');
const bridge = window.vibesysWelcome;
if (bridge === undefined) {
  createRoot(root).render(
    <p className="status error">This page runs inside the VibeSys desktop app.</p>,
  );
} else {
  void load(root, bridge);
}
