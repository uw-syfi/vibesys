/**
 * The host panel's store: one host's checkout and the checkout form, plus sign-in. Its runs are
 * not asked for: the main process pushes them (`lists.ts`).
 *
 * The panel always names the host it shows, and every reply carries the token of the request it
 * answers: a reply for a host no longer shown, or for a request a newer one replaced, is dropped.
 * Which host is shown changes only through `show` (the host list, "Back to This Mac", a Recent
 * row, or the main process), never through a URL. The panel reloads, keeping what it shows, when
 * the window comes back from a run (the run's host is shown).
 */

import {only, type Step} from '@vibesys/core-state';
import type {HostKey} from '../../../common/hosts.js';
import type {ChromeState, WelcomeHost, WelcomeResult} from '../../../common/welcome-protocol.js';

/** The checkout form: "Where is your VibeSys checkout on HOST?". */
export interface CheckoutForm {
  readonly value: string;
  /** False when the host has no checkout yet: the form is the only way on. */
  readonly cancellable: boolean;
  readonly checking: number | null;
  readonly error: string | null;
  readonly authNeeded: boolean;
}

type Panel =
  | {readonly kind: 'loading'; readonly token: number}
  | {
      readonly kind: 'failed';
      readonly error: string;
      readonly authNeeded: boolean;
    }
  | {
      readonly kind: 'ready';
      readonly data: WelcomeHost;
      /** A reload in flight that keeps the panel drawn meanwhile. */
      readonly reloading: number | null;
      readonly checkout: CheckoutForm | null;
    };

export interface HostState {
  readonly next: number;
  readonly host: HostKey;
  /** Which view fills the window, to notice a return from a run. */
  readonly mode: ChromeState['mode'];
  readonly panel: Panel;
  readonly signIn: {readonly token: number} | {readonly error: string} | null;
  /** Cancelled forms whose main-process commit result is still authoritative if it succeeded. */
  readonly abandonedCheckouts: Readonly<Record<number, HostKey>>;
}

export type HostAction =
  | {readonly type: 'show'; readonly host: HostKey}
  | {readonly type: 'reload'}
  | {readonly type: 'chrome'; readonly chrome: ChromeState}
  | {readonly type: 'edit-checkout'}
  | {readonly type: 'checkout-input'; readonly value: string}
  | {readonly type: 'cancel-checkout'}
  | {readonly type: 'submit-checkout'}
  | {readonly type: 'sign-in'}
  | {
      readonly type: 'host-reply';
      readonly token: number;
      readonly result: WelcomeResult<WelcomeHost>;
    }
  | {
      readonly type: 'checkout-reply';
      readonly token: number;
      readonly result: WelcomeResult<string>;
    }
  | {readonly type: 'sign-in-reply'; readonly token: number; readonly result: WelcomeResult<null>};

export type HostRequest =
  | {readonly kind: 'host'; readonly token: number; readonly host: HostKey}
  | {
      readonly kind: 'set-checkout';
      readonly token: number;
      readonly host: HostKey;
      readonly path: string;
    }
  | {readonly kind: 'cancel-checkout'; readonly token: number; readonly host: HostKey}
  | {readonly kind: 'sign-in'; readonly token: number; readonly host: HostKey};

export function hostLabelOf(key: HostKey): string {
  return key === 'local' ? 'This Mac' : key.slice('ssh:'.length);
}

/** The initial state; the shell dispatches `show` for the first host. */
export function hostInitial(host: HostKey): HostState {
  return {
    next: 1,
    host,
    mode: 'welcome',
    panel: {kind: 'loading', token: 0},
    signIn: null,
    abandonedCheckouts: {},
  };
}

/** The tokens the state awaits, by request slot. */
export function hostAwaited(state: HostState): {
  host: number | null;
  checkout: number | null;
  signIn: number | null;
} {
  const panel = state.panel;
  return {
    host: panel.kind === 'loading' ? panel.token : panel.kind === 'ready' ? panel.reloading : null,
    checkout: panel.kind === 'ready' ? (panel.checkout?.checking ?? null) : null,
    signIn: state.signIn !== null && 'token' in state.signIn ? state.signIn.token : null,
  };
}

/** What must hold after every step; returns the violations. */
export function hostInvariants(state: HostState): string[] {
  const problems: string[] = [];
  const panel = state.panel;
  if (panel.kind === 'ready') {
    if (panel.data.key !== state.host)
      problems.push(`panel of ${panel.data.key} under ${state.host}`);
    if (panel.data.checkout === null && panel.checkout === null) {
      problems.push('a host without a checkout shows no checkout form');
    }
    if (panel.data.checkout === null && panel.checkout?.cancellable === true) {
      problems.push('the first checkout form can be cancelled');
    }
  }
  return problems;
}

function load(state: HostState, host: HostKey, quiet: boolean): Step<HostState, HostRequest> {
  const token = state.next;
  const keep = quiet && host === state.host && state.panel.kind === 'ready';
  const panel: Panel =
    keep && state.panel.kind === 'ready'
      ? {...state.panel, reloading: token}
      : {kind: 'loading', token};
  const checking = keep ? null : hostAwaited(state).checkout;
  const form = state.panel.kind === 'ready' ? state.panel.checkout : null;
  return {
    state: {
      ...state,
      next: token + 1,
      host,
      panel,
      signIn: host === state.host ? state.signIn : null,
      abandonedCheckouts:
        checking === null || form === null
          ? state.abandonedCheckouts
          : {
              ...state.abandonedCheckouts,
              [checking]: state.host,
            },
    },
    requests: [
      ...(checking === null
        ? []
        : [{kind: 'cancel-checkout' as const, token: checking, host: state.host}]),
      {kind: 'host', token, host},
    ],
  };
}

function withForm(
  state: HostState,
  change: (form: CheckoutForm, data: WelcomeHost) => CheckoutForm | null,
): HostState {
  const panel = state.panel;
  if (panel.kind !== 'ready' || panel.checkout === null) return state;
  const checkout = change(panel.checkout, panel.data);
  return checkout === panel.checkout ? state : {...state, panel: {...panel, checkout}};
}

function blankForm(value: string, cancellable: boolean): CheckoutForm {
  return {value, cancellable, checking: null, error: null, authNeeded: false};
}

/**
 * The form a reply's host shows: an open form survives a quiet reload, and a host without a
 * checkout always shows the (uncancellable) one asking for it.
 */
function formFor(data: WelcomeHost, kept: CheckoutForm | null): CheckoutForm | null {
  if (data.checkout !== null) return kept?.cancellable === true ? kept : null;
  return kept?.cancellable === false ? kept : blankForm('', false);
}

function hostReply(state: HostState, token: number, result: WelcomeResult<WelcomeHost>): HostState {
  const panel = state.panel;
  if (hostAwaited(state).host !== token) return state;
  if (!result.ok) {
    // A failed quiet reload keeps what the panel showed; a failed load says why.
    if (panel.kind === 'ready') return {...state, panel: {...panel, reloading: null}};
    return {...state, panel: {kind: 'failed', error: result.error, authNeeded: result.authNeeded}};
  }
  const kept = panel.kind === 'ready' ? panel.checkout : null;
  const checkout = formFor(result.value, kept);
  return {...state, panel: {kind: 'ready', data: result.value, reloading: null, checkout}};
}

/** Back from a run: the lists were drawn before the run view covered them; show the run's host. */
function chrome(state: HostState, chromeState: ChromeState): Step<HostState, HostRequest> {
  const next = {...state, mode: chromeState.mode};
  if (state.mode !== 'run' || chromeState.mode !== 'welcome') return only(next);
  const host = chromeState.attached?.host ?? state.host;
  return load(next, host, host === state.host);
}

function editCheckout(state: HostState): HostState {
  const panel = state.panel;
  if (panel.kind !== 'ready' || panel.checkout !== null || panel.data.checkout === null) {
    return state;
  }
  return {...state, panel: {...panel, checkout: blankForm(panel.data.checkout, true)}};
}

function submitCheckout(state: HostState): Step<HostState, HostRequest> {
  const panel = state.panel;
  if (panel.kind !== 'ready' || panel.checkout === null || panel.checkout.checking !== null) {
    return only(state);
  }
  const token = state.next;
  const path = panel.checkout.value;
  const checkout = {...panel.checkout, checking: token, error: null, authNeeded: false};
  return {
    state: {...state, next: token + 1, panel: {...panel, checkout}},
    requests: [{kind: 'set-checkout', token, host: state.host, path}],
  };
}

function cancelCheckout(state: HostState): Step<HostState, HostRequest> {
  const panel = state.panel;
  const form = panel.kind === 'ready' ? panel.checkout : null;
  if (form?.cancellable !== true) return only(state);
  return {
    state:
      form.checking === null
        ? withForm(state, () => null)
        : {
            ...withForm(state, () => null),
            abandonedCheckouts: {
              ...state.abandonedCheckouts,
              [form.checking]: state.host,
            },
          },
    requests:
      form.checking === null
        ? []
        : [{kind: 'cancel-checkout', token: form.checking, host: state.host}],
  };
}

function signIn(state: HostState): Step<HostState, HostRequest> {
  if (state.signIn !== null && 'token' in state.signIn) return only(state);
  const token = state.next;
  return {
    state: {...state, next: token + 1, signIn: {token}},
    requests: [{kind: 'sign-in', token, host: state.host}],
  };
}

function checkoutReply(
  state: HostState,
  token: number,
  result: WelcomeResult<string>,
): Step<HostState, HostRequest> {
  const abandonedHost = state.abandonedCheckouts[token];
  if (abandonedHost !== undefined) {
    const abandonedCheckouts = {...state.abandonedCheckouts};
    delete abandonedCheckouts[token];
    const cleared = {...state, abandonedCheckouts};
    if (!result.ok || abandonedHost !== cleared.host) return only(cleared);
    // The main process may normalize the submitted path. Reload its saved value while preserving
    // any newer form the user has already opened over this panel.
    return load(cleared, cleared.host, true);
  }
  if (hostAwaited(state).checkout !== token) return only(state);
  if (!result.ok) {
    const failed = {checking: null, error: result.error, authNeeded: result.authNeeded};
    return only(withForm(state, form => ({...form, ...failed})));
  }
  // Saved: the form closes and the panel loads from the new checkout.
  return load(
    withForm(state, () => null),
    state.host,
    false,
  );
}

function signInReply(
  state: HostState,
  token: number,
  result: WelcomeResult<null>,
): Step<HostState, HostRequest> {
  if (hostAwaited(state).signIn !== token) return only(state);
  if (!result.ok) return only({...state, signIn: {error: result.error}});
  return load({...state, signIn: null}, state.host, false);
}

export function hostStep(state: HostState, action: HostAction): Step<HostState, HostRequest> {
  switch (action.type) {
    case 'show':
      return load(state, action.host, false);
    case 'reload':
      return load(state, state.host, false);
    case 'chrome':
      return chrome(state, action.chrome);
    case 'edit-checkout':
      return only(editCheckout(state));
    case 'checkout-input':
      return only(withForm(state, form => ({...form, value: action.value})));
    case 'cancel-checkout':
      return cancelCheckout(state);
    case 'submit-checkout':
      return submitCheckout(state);
    case 'sign-in':
      return signIn(state);
    case 'host-reply':
      return only(hostReply(state, action.token, action.result));
    case 'checkout-reply':
      return checkoutReply(state, action.token, action.result);
    case 'sign-in-reply':
      return signInReply(state, action.token, action.result);
  }
}
