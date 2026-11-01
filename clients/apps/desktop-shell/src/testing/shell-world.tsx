/**
 * The app shell drawn in a DOM over the Fake bridge of the browser journeys: the same world
 * (`seed.ts`) and the same Fakes (`fake-bridge.ts`, which the browser journeys install in the page), the shell's own stores and screens, no
 * browser. A test that holds behavior and not layout opens the shell here, in about 25 ms, instead
 * of in Chromium, in about 500 ms. Call it where a happy-dom `window` is the global one.
 */
import type {Screen} from '@vibesys/feature-app-shell/state';
import {browserDraftMemory, browserMoreOptionsMemory} from '@vibesys/feature-new-run/renderer';
import {DRAFT_KEY, type Draft, storeDraft} from '@vibesys/feature-new-run/state';
import type {WelcomeBridge} from '@vibesys/ipc';
import type {ScheduleTimeout} from '@vibesys/transport';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {createShellStores, Shell} from '../shell.js';
import {FakeWindowAddress} from './fake-address.js';
import {
  type FakeBridgeSeed,
  installFakeBridge,
  installFakeChrome,
  installFakeExampleDestination,
  installFakeMachineSettings,
  installFakeMachines,
  installFakeNewRun,
  installFakeProjectRuns,
} from './fake-bridge.js';
import {SEED} from './seed.js';

/** The bounded number of turns a window gets to read what it shows before the test says it never did. */
const READ_TURNS = 64;

export interface OpenShell {
  readonly container: HTMLElement;
  readonly stores: ReturnType<typeof createShellStores>;
  /** Returns once the page says it has read what it shows (`data-state="ready"`). */
  readonly read: () => Promise<void>;
  readonly close: () => Promise<void>;
}

const ignore = (): void => undefined;

/**
 * The app opened on `screen` over the world `seed`, with `draft` as this window's New run; with
 * no `screen` it opens as it does at launch, on the screen the window's address and the runs it
 * finds decide.
 */
export async function openShell(
  screen: Screen | null,
  options: {
    readonly schedule: ScheduleTimeout;
    readonly seed?: FakeBridgeSeed;
    readonly draft?: Draft;
    /** The window's clock, in milliseconds (default 0, the start of time: a page that cuts days wants a real one). */
    readonly now?: number;
    /** Keeps the window's storage as it is, as a reload does, instead of starting it empty. */
    readonly keepStorage?: boolean;
    /** Fakes of more of the bridge, installed after the usual ones (what the Usage page asks of a host). */
    readonly installs?: readonly ((seed: FakeBridgeSeed) => void)[];
    /** Changes the bridge before the shell reads from it (a call a bridge built earlier lacks). */
    readonly prepare?: (bridge: Record<string, unknown>) => void;
  },
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
    ...(options.installs ?? []),
  ]) {
    install(seed);
  }
  const bridge = host.vibesysWelcome;
  if (bridge === undefined) throw new Error('the Fake bridge installed no window.vibesysWelcome');
  options.prepare?.(bridge as unknown as Record<string, unknown>);
  if (options.keepStorage !== true) {
    host.sessionStorage.clear();
    host.localStorage.clear();
  }
  if (options.draft !== undefined)
    host.sessionStorage.setItem(DRAFT_KEY, storeDraft(options.draft));
  const stores = createShellStores(
    bridge,
    {mode: 'light', onChange: ignore},
    {setAttribute: ignore},
    options.schedule,
    {
      messages: {addEventListener: ignore, removeEventListener: ignore},
      address: new FakeWindowAddress(),
      now: () => options.now ?? 0,
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
  if (screen !== null) {
    await act(async () => stores.sidebar.store.dispatch({type: 'select', screen}));
  }
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
    read,
    close: async () => {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}
