/**
 * The replay page's "Live gateway URL" form: the URL typed, then one capability exchange at a time.
 * A submit validates the URL against the page (`targetFromCapability`), asks the shell to exchange
 * its capability (`bootstrap`), and on the reply's success hands the connection to the page owner
 * (`connected`), which remembers it and goes live. A second submit while one exchange is pending is
 * ignored, so one capability is never exchanged twice.
 */
import {
  type GatewayConnection,
  type GatewayTarget,
  targetFromCapability,
} from '../../../entities/gateway.js';
import {only, type Step} from '../../../shared/step.js';

export type GatewayConnectState =
  | {
      readonly kind: 'editing';
      readonly page: string;
      readonly url: string;
      readonly error: string | null;
      readonly next: number;
    }
  | {
      readonly kind: 'connecting';
      readonly page: string;
      readonly url: string;
      readonly target: GatewayTarget;
      readonly token: number;
      readonly next: number;
    }
  /** Handed off: the page is going live and the form stays inert. */
  | {
      readonly kind: 'connected';
      readonly page: string;
      readonly url: string;
      readonly next: number;
    };

type BootstrapResult =
  | {readonly ok: true; readonly browserSession: string}
  | {readonly ok: false; readonly error: string};

export type GatewayConnectAction =
  | {readonly type: 'edit'; readonly value: string}
  | {readonly type: 'submit'}
  | {readonly type: 'bootstrap-reply'; readonly token: number; readonly result: BootstrapResult};

export type GatewayConnectRequest =
  | {readonly kind: 'bootstrap'; readonly token: number; readonly url: string}
  | {readonly kind: 'connected'; readonly connection: GatewayConnection};

/** The form on `page` (the capability-free page URL), showing `error` from a refused launch. */
export function gatewayConnectInitial(page: string, error: string | null): GatewayConnectState {
  return {kind: 'editing', page, url: '', error, next: 1};
}

export function gatewayConnectStep(
  state: GatewayConnectState,
  action: GatewayConnectAction,
): Step<GatewayConnectState, GatewayConnectRequest> {
  switch (action.type) {
    case 'edit':
      return only({...state, url: action.value});
    case 'submit': {
      if (state.kind !== 'editing') return only(state);
      let target: GatewayTarget;
      try {
        target = targetFromCapability(state.page, state.url);
      } catch (reason) {
        return only({...state, error: reason instanceof Error ? reason.message : String(reason)});
      }
      if (target.bootstrapUrl === null) {
        return only({...state, error: 'The gateway URL has no capability token'});
      }
      const token = state.next;
      return {
        state: {
          kind: 'connecting',
          page: state.page,
          url: state.url,
          target,
          token,
          next: token + 1,
        },
        requests: [{kind: 'bootstrap', token, url: target.bootstrapUrl}],
      };
    }
    case 'bootstrap-reply': {
      if (state.kind !== 'connecting' || state.token !== action.token) return only(state);
      const {page, url, next, target} = state;
      if (!action.result.ok) {
        return only({kind: 'editing', page, url, error: action.result.error, next});
      }
      return {
        state: {kind: 'connected', page, url, next},
        requests: [
          {
            kind: 'connected',
            connection: {
              gatewayUrl: target.gatewayUrl,
              cleanPageUrl: target.cleanPageUrl,
              webSocketUrl: target.webSocketUrl,
              browserSession: action.result.browserSession,
            },
          },
        ],
      };
    }
  }
}

export function gatewayConnectInvariants(state: GatewayConnectState): string[] {
  const problems: string[] = [];
  if (state.kind === 'connecting') {
    if (state.token >= state.next) problems.push('token not issued');
    if (state.target.bootstrapUrl === null) problems.push('connecting without a capability');
  }
  if (state.kind !== 'editing' && 'error' in state) problems.push('error outside editing');
  return problems;
}
