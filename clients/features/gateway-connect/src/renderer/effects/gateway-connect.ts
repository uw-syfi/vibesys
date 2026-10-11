/**
 * The gateway form's shell: it runs the form's core (`state/gateway-connect.ts`), exchanges the
 * capability through the `bootstrap` the composition injects, and hands a connection to the page owner, which remembers it
 * and goes live.
 */
import {createStore, type Store} from '@vibesys/store';
import type {GatewayBootstrapTarget, GatewayConnection} from '../state/gateway.js';
import {
  type GatewayConnectAction,
  type GatewayConnectState,
  gatewayConnectInitial,
  gatewayConnectStep,
} from '../state/gateway-connect.js';

export interface GatewayConnectOptions {
  /** The capability-free page URL a pasted capability is resolved against. */
  readonly page: string;
  readonly initialError: string | null;
  /** A launch target that starts immediately, while the first shell render remains synchronous. */
  readonly initialTarget?: GatewayBootstrapTarget | null;
  /** Report an authenticated target while its credential still exists in this document. */
  readonly onConnected: (connection: GatewayConnection) => void;
  /** Exchange a launch capability for a browser-session credential. */
  readonly bootstrap: (capabilityUrl: string, signal: AbortSignal) => Promise<string>;
}

export function createGatewayConnectStore({
  page,
  initialError,
  initialTarget = null,
  onConnected,
  bootstrap,
}: GatewayConnectOptions): Store<GatewayConnectState, GatewayConnectAction> {
  const attempts = new Map<number, AbortController>();
  const store = createStore(
    gatewayConnectStep,
    gatewayConnectInitial(page, initialError),
    (request, dispatch) => {
      if (request.kind === 'connected') {
        onConnected(request.connection);
        return;
      }
      if (request.kind === 'cancel-bootstrap') {
        const controller = attempts.get(request.token);
        attempts.delete(request.token);
        controller?.abort(new Error('Gateway connection cancelled'));
        return;
      }
      const {token} = request;
      const controller = new AbortController();
      attempts.set(token, controller);
      let exchange: Promise<string>;
      try {
        exchange = bootstrap(request.url, controller.signal);
      } catch (reason) {
        attempts.delete(token);
        dispatch({
          type: 'bootstrap-reply',
          token,
          result: {ok: false, error: reason instanceof Error ? reason.message : String(reason)},
        });
        return;
      }
      void exchange
        .then(
          browserSession =>
            dispatch({type: 'bootstrap-reply', token, result: {ok: true, browserSession}}),
          (reason: unknown) =>
            dispatch({
              type: 'bootstrap-reply',
              token,
              result: {ok: false, error: reason instanceof Error ? reason.message : String(reason)},
            }),
        )
        .finally(() => {
          if (attempts.get(token) === controller) attempts.delete(token);
        });
    },
  );
  if (initialTarget !== null) store.dispatch({type: 'connect', target: initialTarget});
  return store;
}
