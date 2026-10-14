/**
 * The run page's composition: it builds the feature stores, subscribes React to them, and renders
 * the features' presentational views with their state and dispatchers. Cross-feature flows are
 * wired here (replayed events fold into the run view; a connected gateway goes to the page owner).
 * Wiring only; the entries (`main.tsx`, `desktop-main.tsx`) decide which page to compose.
 */

import {
  createGatewayConnectStore,
  type GatewayBootstrapTarget,
  type GatewayConnectAction,
  type GatewayConnection,
  type GatewayConnectState,
  GatewayForm,
} from '@vibesys/feature-gateway-connect/renderer';
import {createReplayStore} from '@vibesys/feature-replay/renderer';
import {type ReplayAction, type ReplayState, replayFailure} from '@vibesys/feature-replay/state';
import {RunPage} from '@vibesys/feature-run-view/renderer';
import {createRunStore, type RunStore, type WebSession} from '@vibesys/run-session';
import type {Store} from '@vibesys/store';
import type {JSX, ReactNode} from 'react';
import {useSyncExternalStore} from 'react';
import {bootstrapGateway, fetchReplayFixture} from '../platform/backend.js';
import type {WebThemeController} from './theme.js';
import {WebThemePicker} from './theme-picker.js';
// Each feature styles its own views; the page frame is the entry's `styles.css`.
import '@vibesys/feature-gateway-connect/styles.css';
import '@vibesys/feature-run-view/styles.css';

function useStore<S>(store: Pick<Store<S, unknown>, 'getState' | 'subscribe'>): S {
  return useSyncExternalStore(store.subscribe, store.getState, store.getState);
}

const NOTHING = (): void => undefined;

export interface DemoAppOptions {
  /** The capability-free page URL a pasted capability is resolved against. */
  readonly page: string;
  /** Report an authenticated target while its credential still exists in this document. */
  readonly onGatewayConnected: (connection: GatewayConnection) => void;
  readonly initialGatewayError?: string | null;
  /** Begin this already-validated launch without delaying the first render. */
  readonly initialGatewayTarget?: GatewayBootstrapTarget | null;
  /** The page-owned preference survives replacement of replay and live run surfaces. */
  readonly theme: WebThemeController;
}

export interface DemoAppServices {
  readonly bootstrap: (capabilityUrl: string, signal: AbortSignal) => Promise<string>;
  readonly loadReplay: typeof fetchReplayFixture;
}

const DEFAULT_DEMO_SERVICES: DemoAppServices = {
  bootstrap: (capabilityUrl, signal) => bootstrapGateway(capabilityUrl, {signal}),
  loadReplay: fetchReplayFixture,
};

/** The replay demo, with the form that switches the page to a live gateway. */
export function createDemoApp(
  {
    page,
    onGatewayConnected,
    initialGatewayError = null,
    initialGatewayTarget = null,
    theme,
  }: DemoAppOptions,
  services: DemoAppServices = DEFAULT_DEMO_SERVICES,
): JSX.Element {
  const run = createRunStore();
  const replay = createReplayStore(
    events => run.dispatch({type: 'events', events}),
    services.loadReplay,
  );
  const gateway = createGatewayConnectStore({
    page,
    initialError: initialGatewayError,
    initialTarget: initialGatewayTarget,
    onConnected: onGatewayConnected,
    bootstrap: services.bootstrap,
  });
  replay.dispatch({type: 'load'});
  return (
    <WebAppShell theme={theme}>
      <Demo run={run} replay={replay} gateway={gateway} />
    </WebAppShell>
  );
}

function Demo({
  run,
  replay,
  gateway,
}: {
  readonly run: RunStore;
  readonly replay: Store<ReplayState, ReplayAction>;
  readonly gateway: Store<GatewayConnectState, GatewayConnectAction>;
}): JSX.Element {
  const iterations = useSyncExternalStore(run.subscribe, run.getIterations, run.getIterations);
  return (
    <>
      <GatewayForm state={useStore(gateway)} send={gateway.dispatch} />
      <RunPage
        run={useStore(run)}
        iterations={iterations}
        session={null}
        replayFailure={replayFailure(useStore(replay))}
        onReattach={NOTHING}
        onReconnectControls={NOTHING}
        onRetryReplay={() => replay.dispatch({type: 'load'})}
        onCommand={null}
      />
    </>
  );
}

/** The live page over `session`, which this starts. */
export function createLiveApp(session: WebSession, theme: WebThemeController): JSX.Element {
  void session.start();
  return (
    <WebAppShell theme={theme}>
      <Live session={session} run={session.store} />
    </WebAppShell>
  );
}

function WebAppShell({
  theme,
  children,
}: {
  readonly theme: WebThemeController;
  readonly children: ReactNode;
}): JSX.Element {
  return (
    <>
      <WebThemePicker theme={theme} />
      {children}
    </>
  );
}

function Live({session, run}: {readonly session: WebSession; readonly run: RunStore}): JSX.Element {
  const iterations = useSyncExternalStore(run.subscribe, run.getIterations, run.getIterations);
  return (
    <RunPage
      run={useStore(run)}
      iterations={iterations}
      session={useStore(session)}
      replayFailure={null}
      onReattach={() => session.reattach()}
      onReconnectControls={() => session.reconnectControls()}
      onRetryReplay={NOTHING}
      onCommand={session.command}
    />
  );
}
