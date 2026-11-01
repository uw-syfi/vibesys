/**
 * The app shell drawn in a DOM over the Fake bridge of the browser journeys: the same world
 * (`seed.ts`) and the same Fakes (`fake-bridge.ts`, which the browser journeys install in the page), the shell's own stores and screens, no
 * browser. A test that holds behavior and not layout opens the shell here, in about 25 ms, instead
 * of in Chromium, in about 500 ms. Call it where a happy-dom `window` is the global one.
 */
import type {Screen} from '@vibesys/feature-app-shell/state';
import {browserDraftMemory, browserMoreOptionsMemory} from '@vibesys/feature-new-run/renderer';
import {DRAFT_KEY, type Draft, storeDraft} from '@vibesys/feature-new-run/state';
import {subscriptionBridge, type WelcomeBridge} from '@vibesys/ipc';
import type {ScheduleTimeout} from '@vibesys/transport';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {createShellStores, Shell, wakeRail} from '../shell.js';
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
import {type FakeGateway, installGateway} from './gateway-preload.js';
import {connectUpdates, type ReplayWorld} from './replay-world.js';
import {SEED} from './seed.js';
import {installSubscriptions, type SubscriptionSeed} from './subscription-preload.js';

/** The bounded number of turns a window gets to read what it shows before the test says it never did. */
const READ_TURNS = 64;

export interface OpenShell {
  readonly container: HTMLElement;
  readonly stores: ReturnType<typeof createShellStores>;
  /** The Fake gateway the window's Run screen reads, when the shell was opened over a run (`options.run`). */
  readonly gateway: FakeGateway | null;
  /** Delivers the live run's next records, as the browser journeys' `playLive` does (`options.run.updates`). */
  readonly emit: (sources: readonly number[]) => void;
  /** Returns once the page says it has read what it shows (`data-state="ready"`). */
  readonly read: () => Promise<void>;
  readonly close: () => Promise<void>;
}

const ignore = (): void => undefined;

/** The window's second port over the hosts `options` script, timed by the window's own clock. */
function subscriptionPort(options: {
  readonly subscriptions?: SubscriptionSeed;
  readonly now?: number;
}) {
  if (options.subscriptions === undefined) throw new Error('no hosts are scripted');
  installSubscriptions(options.subscriptions, () => options.now ?? 0);
  const host = globalThis.window as unknown as Window & {
    vibesysSubscription?: Parameters<typeof subscriptionBridge>[0];
  };
  if (host.vibesysSubscription === undefined) throw new Error('the Fake installed no port');
  return subscriptionBridge(host.vibesysSubscription);
}

/**
 * Installs the Fake gateway on the window as the Electron preload does, and returns its second
 * port (the run's liveness), the controls the journeys reach as `window.fakeGateway`, and the way
 * the live run's later records arrive.
 */
function serveRun(
  host: Window,
  world: ReplayWorld,
): {
  readonly port: ReturnType<typeof subscriptionBridge>;
  readonly controls: FakeGateway;
  readonly emit: (sources: readonly number[]) => void;
} {
  installGateway(world.input);
  const served = host as unknown as Window & {
    vibesysSubscription?: Parameters<typeof subscriptionBridge>[0];
    fakeGateway?: FakeGateway;
    fakeHost?: {emit?: (events: readonly {sequence?: number}[]) => void};
  };
  if (served.vibesysSubscription === undefined || served.fakeGateway === undefined)
    throw new Error('the Fake gateway installed no ports');
  connectUpdates(world.updates);
  const emit = served.fakeHost?.emit;
  if (emit === undefined) throw new Error('the Fake gateway installed no way to deliver records');
  return {
    port: subscriptionBridge(served.vibesysSubscription),
    controls: served.fakeGateway,
    emit: sources => emit(sources.map(sequence => ({sequence}))),
  };
}

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
    /** The hosts' live data the window's second port serves (their machine stats and their runs). */
    readonly subscriptions?: SubscriptionSeed;
    /**
     * The run the window's Run screen reads: the browser journeys' Fake gateway (`gateway-preload.ts`)
     * serving `world.input`, which is also the window's port to the run's liveness. Open the run as
     * a person does, from the dashboard (`openRun`). Excludes `subscriptions`.
     */
    readonly run?: ReplayWorld;
    /** Fakes of more of the bridge, installed after the usual ones (what the Usage page asks of a host). */
    readonly installs?: readonly ((seed: FakeBridgeSeed) => void)[];
    /** Changes the bridge before the shell reads from it (a call a bridge built earlier lacks). */
    readonly prepare?: (bridge: Record<string, unknown>) => void;
  },
): Promise<OpenShell> {
  const seed = options.seed ?? SEED;
  const host = globalThis.window as unknown as Window & {vibesysWelcome?: WelcomeBridge};
  if (options.run !== undefined && options.subscriptions !== undefined)
    throw new Error('the window has one port to the hosts: a run or their scripted data, not both');
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
  const gateway = options.run === undefined ? null : serveRun(host, options.run);
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
      ...(options.subscriptions === undefined ? {} : {subscriptions: subscriptionPort(options)}),
      ...(gateway === null ? {} : {subscriptions: gateway.port}),
      session: () => {
        throw new Error('no run is opened in this window');
      },
    },
    {
      moreOptions: browserMoreOptionsMemory(() => host.localStorage),
      draft: browserDraftMemory(() => host.sessionStorage),
    },
  );
  // As the app does, once the stores exist: the rail's machine checks start when Runs has read.
  wakeRail(stores);
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
    gateway: gateway?.controls ?? null,
    emit: sources => gateway?.emit(sources),
    read,
    close: async () => {
      await act(async () => root.unmount());
      container.remove();
    },
  };
}
