/**
 * The welcome view's composition root: it builds the screen stores over the welcome preload's
 * bridge (`effects/`), subscribes React to them, and renders the presentational view (`ui/`) with
 * each store's state and dispatcher.
 */

import type {Store} from '@vibesys/core-state';
import {type JSX, StrictMode, useSyncExternalStore} from 'react';
import {createRoot} from 'react-dom/client';
import type {WelcomeBridge} from '../../common/welcome-protocol.js';
import {type WelcomeStores, welcomeStores} from './effects/stores.js';
import {WelcomeView} from './ui/welcome-view.js';
import './ui/welcome.css';

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
  const overview = await bridge.overview();
  if (!overview.ok) {
    reactRoot.render(<p className="status error">{overview.error}</p>);
    return;
  }
  const {chrome, recent, settingsProblem, host} = overview.value;
  const stores = welcomeStores(bridge, question => window.confirm(question), host);
  stores.welcome.dispatch({type: 'chrome', chrome});
  stores.host.dispatch({type: 'chrome', chrome});
  stores.lists.dispatch({type: 'runs', runs: recent});
  if (settingsProblem !== null) stores.welcome.dispatch({type: 'problem', text: settingsProblem});
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
