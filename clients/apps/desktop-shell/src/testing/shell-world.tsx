/**
 * The app shell drawn in a DOM over the Fake bridge of the browser journeys: the same world
 * (`e2e/seed.ts`) and the same Fakes (`e2e/fake-bridge.ts`), the shell's own stores and screens, no
 * browser. A test that holds behavior and not layout opens the shell here, in about 25 ms, instead
 * of in Chromium, in about 500 ms. Call it where a happy-dom `window` is the global one.
 */
import type {Screen} from '@vibesys/feature-app-shell/state';
import {browserDraftMemory, browserMoreOptionsMemory} from '@vibesys/feature-new-run/renderer';
import {DRAFT_KEY, type Draft, storeDraft} from '@vibesys/feature-new-run/state';
import type {WelcomeBridge} from '@vibesys/ipc';
import {FakeClock} from '@vibesys/transport/testing';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {
  type FakeBridgeSeed,
  installFakeBridge,
  installFakeChrome,
  installFakeExampleDestination,
  installFakeMachineSettings,
  installFakeMachines,
  installFakeNewRun,
  installFakeProjectRuns,
} from '../../e2e/fake-bridge.js';
import {SEED} from '../../e2e/seed.js';
import {createShellStores, Shell} from '../shell.js';
import {FakeWindowAddress} from './fake-address.js';

/** The bounded number of turns a window gets to read what it shows before the test says it never did. */
const READ_TURNS = 64;

export interface OpenShell {
  readonly container: HTMLElement;
  readonly stores: ReturnType<typeof createShellStores>;
  readonly clock: FakeClock;
  /** Returns once the page says it has read what it shows (`data-state="ready"`). */
  readonly read: () => Promise<void>;
  readonly close: () => Promise<void>;
}

const ignore = (): void => undefined;

/** The app opened on `screen` over the world `seed`, with `draft` as this window's New run. */
export async function openShell(
  screen: Screen,
  options: {readonly seed?: FakeBridgeSeed; readonly draft?: Draft} = {},
): Promise<OpenShell> {
  const seed = options.seed ?? SEED;
  const host = globalThis.window as unknown as Window & {vibesysWelcome?: WelcomeBridge};
  for (const install of [
    installFakeChrome,
    installFakeBridge,
    installFakeMachines,
    installFakeMachineSettings,
    installFakeNewRun,
    installFakeProjectRuns,
    installFakeExampleDestination,
  ]) {
    install(seed);
  }
  const bridge = host.vibesysWelcome;
  if (bridge === undefined) throw new Error('the Fake bridge installed no window.vibesysWelcome');
  host.sessionStorage.clear();
  host.localStorage.clear();
  if (options.draft !== undefined)
    host.sessionStorage.setItem(DRAFT_KEY, storeDraft(options.draft));
  const clock = new FakeClock();
  const stores = createShellStores(
    bridge,
    {mode: 'light', onChange: ignore},
    {setAttribute: ignore},
    clock.schedule,
    {
      messages: {addEventListener: ignore, removeEventListener: ignore},
      address: new FakeWindowAddress(),
      now: () => 0,
      session: () => {
        throw new Error('no run is opened in this window');
      },
    },
    {
      moreOptions: browserMoreOptionsMemory(() => host.localStorage),
      draft: browserDraftMemory(() => host.sessionStorage),
    },
  );
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(<Shell stores={stores} version="0" />));
  await act(async () => stores.sidebar.store.dispatch({type: 'select', screen}));
  const read = async (): Promise<void> => {
    for (let turn = 0; turn < READ_TURNS; turn += 1) {
      await act(async () => Promise.resolve());
      const page = container.querySelector('main[data-state]');
      if (page?.getAttribute('data-state') === 'ready') return;
    }
    throw new Error(`the screen did not say it had read what it shows in ${READ_TURNS} turns`);
  };
  await read();
  return {
    container,
    stores,
    clock,
    read,
    close: async () => {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}
