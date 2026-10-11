/**
 * The gateway form's shell: it runs the form's core (`state/gateway-connect.ts`), exchanges the
 * capability through the `bootstrap` the composition injects, and hands a connection to the page owner, which remembers it
 * and goes live.
 */
import type {GatewayConnection} from '../../../entities/gateway.js';
import {createStore, type Store} from '../../../shared/store.js';
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
  /** Report an authenticated target while its credential still exists in this document. */
  readonly onConnected: (connection: GatewayConnection) => void;
  /** Exchange a launch capability for a browser-session credential. */
  readonly bootstrap: (capabilityUrl: string) => Promise<string>;
}

export function createGatewayConnectStore({
  page,
  initialError,
  onConnected,
  bootstrap,
}: GatewayConnectOptions): Store<GatewayConnectState, GatewayConnectAction> {
  return createStore(
    gatewayConnectStep,
    gatewayConnectInitial(page, initialError),
    (request, dispatch) => {
      if (request.kind === 'connected') {
        onConnected(request.connection);
        return;
      }
      const {token} = request;
      bootstrap(request.url).then(
        browserSession =>
          dispatch({type: 'bootstrap-reply', token, result: {ok: true, browserSession}}),
        (reason: unknown) =>
          dispatch({
            type: 'bootstrap-reply',
            token,
            result: {ok: false, error: reason instanceof Error ? reason.message : String(reason)},
          }),
      );
    },
  );
}
