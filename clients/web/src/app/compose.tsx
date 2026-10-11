/**
 * The run page's composition: it builds the feature stores, subscribes React to them, and renders
 * the features' presentational views with their state and dispatchers. Cross-feature flows are
 * wired here (replayed events fold into the run view; a connected gateway goes to the page owner).
 * Wiring only; the entries (`main.tsx`, `desktop-main.tsx`) decide which page to compose.
 */

import type {Store} from '@vibesys/core-state';
import type {JSX} from 'react';
import {useSyncExternalStore} from 'react';
import type {GatewayBootstrapTarget, GatewayConnection} from '../entities/gateway.js';
import {
  createGatewayConnectStore,
  type GatewayConnectAction,
  type GatewayConnectState,
  GatewayForm,
} from '../features/gateway-connect/index.js';
import {
  createReplayStore,
  type ReplayAction,
  type ReplayState,
  replayFailure,
} from '../features/replay/index.js';
import {
  createRunStore,
  RunPage,
  type RunStore,
  type WebSession,
} from '../features/run-view/index.js';
import {bootstrapGateway, fetchReplayFixture} from '../platform/backend.js';

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
  return <Demo run={run} replay={replay} gateway={gateway} />;
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
  return (
    <>
      <GatewayForm state={useStore(gateway)} send={gateway.dispatch} />
      <RunPage
        run={useStore(run)}
        session={null}
        replayFailure={replayFailure(useStore(replay))}
        onReattach={NOTHING}
        onReconnectControls={NOTHING}
        onRetryReplay={() => replay.dispatch({type: 'load'})}
      />
    </>
  );
}

/** The live page over `session`, which this starts. */
export function createLiveApp(session: WebSession): JSX.Element {
  void session.start();
  return <Live session={session} run={session.store} />;
}

function Live({session, run}: {readonly session: WebSession; readonly run: RunStore}): JSX.Element {
  return (
    <RunPage
      run={useStore(run)}
      session={useStore(session)}
      replayFailure={null}
      onReattach={() => session.reattach()}
      onReconnectControls={() => session.reconnectControls()}
      onRetryReplay={NOTHING}
    />
  );
}
