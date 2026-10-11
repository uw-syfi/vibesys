/**
 * The replay page's "Live gateway URL" form: the URL typed, then one capability exchange at a time.
 * A submit validates the URL against the page (`targetFromCapability`), asks the shell to exchange
 * its capability (`bootstrap`), and on the reply's success hands the connection to the page owner
 * (`connected`), which remembers it and goes live. Pending attempts can be cancelled; failed and
 * cancelled attempts retain their validated target privately in state for an explicit retry.
 */

import {only, type Step} from '@vibesys/core-state';
import {
  type GatewayBootstrapTarget,
  type GatewayConnection,
  targetFromCapability,
} from './gateway.js';

const CANCELLED_ERROR = 'Gateway connection cancelled. Retry when ready.';

export type GatewayConnectFeedback =
  | {readonly kind: 'failure'; readonly message: string}
  | {readonly kind: 'cancelled'; readonly message: string};

export type GatewayConnectState =
  | {
      readonly kind: 'editing';
      readonly page: string;
      readonly url: string;
      readonly feedback: GatewayConnectFeedback | null;
      /** A validated capability kept for Retry, but not rendered into an initially blank field. */
      readonly retryTarget: GatewayBootstrapTarget | null;
      readonly next: number;
    }
  | {
      readonly kind: 'connecting';
      readonly page: string;
      readonly url: string;
      readonly target: GatewayBootstrapTarget;
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
  /** Start a target already validated by the browser boot decision. */
  | {readonly type: 'connect'; readonly target: GatewayBootstrapTarget}
  | {readonly type: 'retry'}
  | {readonly type: 'cancel'}
  | {readonly type: 'bootstrap-reply'; readonly token: number; readonly result: BootstrapResult};

export type GatewayConnectRequest =
  | {readonly kind: 'bootstrap'; readonly token: number; readonly url: string}
  | {readonly kind: 'cancel-bootstrap'; readonly token: number}
  | {readonly kind: 'connected'; readonly connection: GatewayConnection};

/** The form on `page` (the capability-free page URL), showing a refused launch as failure feedback. */
export function gatewayConnectInitial(page: string, error: string | null): GatewayConnectState {
  return {
    kind: 'editing',
    page,
    url: '',
    feedback: error === null ? null : {kind: 'failure', message: error},
    retryTarget: null,
    next: 1,
  };
}

export function gatewayConnectStep(
  state: GatewayConnectState,
  action: GatewayConnectAction,
): Step<GatewayConnectState, GatewayConnectRequest> {
  switch (action.type) {
    case 'edit':
      return edit(state, action.value);
    case 'submit':
      return submit(state);
    case 'connect':
      return connect(state, action.target);
    case 'retry':
      return retry(state);
    case 'cancel':
      return cancel(state);
    case 'bootstrap-reply':
      return bootstrapReply(state, action.token, action.result);
  }
}

export function gatewayConnectInvariants(state: GatewayConnectState): string[] {
  const problems: string[] = [];
  if (state.kind === 'connecting' && state.target.bootstrapUrl === null) {
    problems.push('connecting without a capability');
  }
  if (state.kind === 'editing' && state.retryTarget?.bootstrapUrl === '') {
    problems.push('retry without a capability');
  }
  if (state.kind !== 'editing' && 'feedback' in state) problems.push('feedback outside editing');
  return problems;
}

function begin(
  state: Extract<GatewayConnectState, {readonly kind: 'editing'}>,
  target: GatewayBootstrapTarget,
): Step<GatewayConnectState, GatewayConnectRequest> {
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

function edit(
  state: GatewayConnectState,
  value: string,
): Step<GatewayConnectState, GatewayConnectRequest> {
  if (state.kind !== 'editing') return only(state);
  return only({...state, url: value, feedback: null, retryTarget: null});
}

function submit(state: GatewayConnectState): Step<GatewayConnectState, GatewayConnectRequest> {
  if (state.kind !== 'editing') return only(state);
  try {
    const target = targetFromCapability(state.page, state.url);
    if (target.bootstrapUrl === null) {
      return only({
        ...state,
        feedback: {kind: 'failure', message: 'The gateway URL has no capability token'},
        retryTarget: null,
      });
    }
    return begin(state, {...target, bootstrapUrl: target.bootstrapUrl});
  } catch (reason) {
    return only({
      ...state,
      feedback: {
        kind: 'failure',
        message: reason instanceof Error ? reason.message : String(reason),
      },
      retryTarget: null,
    });
  }
}

function connect(
  state: GatewayConnectState,
  target: GatewayBootstrapTarget,
): Step<GatewayConnectState, GatewayConnectRequest> {
  return state.kind === 'editing' ? begin(state, target) : only(state);
}

function retry(state: GatewayConnectState): Step<GatewayConnectState, GatewayConnectRequest> {
  if (state.kind !== 'editing' || state.retryTarget === null) return only(state);
  return begin(state, state.retryTarget);
}

function cancel(state: GatewayConnectState): Step<GatewayConnectState, GatewayConnectRequest> {
  if (state.kind !== 'connecting') return only(state);
  return {
    state: {
      kind: 'editing',
      page: state.page,
      url: state.url,
      feedback: {kind: 'cancelled', message: CANCELLED_ERROR},
      retryTarget: state.target,
      next: state.next,
    },
    requests: [{kind: 'cancel-bootstrap', token: state.token}],
  };
}

function bootstrapReply(
  state: GatewayConnectState,
  token: number,
  result: BootstrapResult,
): Step<GatewayConnectState, GatewayConnectRequest> {
  if (state.kind !== 'connecting' || state.token !== token) return only(state);
  const {page, url, next, target} = state;
  if (!result.ok) {
    return only({
      kind: 'editing',
      page,
      url,
      feedback: {kind: 'failure', message: result.error},
      retryTarget: target,
      next,
    });
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
          browserSession: result.browserSession,
        },
      },
    ],
  };
}
