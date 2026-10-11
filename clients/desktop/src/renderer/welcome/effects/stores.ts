/**
 * The welcome view's shell: one store per screen, each request performed through the welcome
 * bridge and its reply dispatched back with the request's token, and the main process's events
 * fanned out to the stores that fold them. This is the only module of the view that talks to the
 * bridge. Nothing here polls: host data arrives as pushes (`onListing`).
 */

import {createStore, type Store} from '@vibesys/core-state';
import type {HostKey} from '../../../common/hosts.js';
import type {WelcomeBridge, WelcomeResult} from '../../../common/welcome-protocol.js';
import {
  COMPLETION_EMPTY,
  type CompletionAction,
  type CompletionField,
  type CompletionState,
  completionStep,
} from '../state/completion.js';
import {type HostAction, type HostState, hostInitial, hostStep} from '../state/host.js';
import {LISTS_EMPTY, type ListsAction, type ListsState, listsStep} from '../state/lists.js';
import {PICKER_CLOSED, type PickerAction, type PickerState, pickerStep} from '../state/picker.js';
import {START_CLOSED, type StartAction, type StartState, startStep} from '../state/start.js';
import {
  type RunCommand,
  WELCOME_INITIAL,
  type WelcomeAction,
  type WelcomeState,
  welcomeStep,
} from '../state/welcome.js';

export interface WelcomeStores {
  readonly welcome: Store<WelcomeState, WelcomeAction>;
  readonly lists: Store<ListsState, ListsAction>;
  readonly host: Store<HostState, HostAction>;
  readonly start: Store<StartState, StartAction>;
  readonly picker: Store<PickerState, PickerAction>;
  readonly completion: Store<CompletionState, CompletionAction>;
}

/** A bridge call that rejected (the main process went away) as a failed result. */
function settled<T>(call: Promise<WelcomeResult<T>>): Promise<WelcomeResult<T>> {
  return call.catch(
    (error: unknown): WelcomeResult<T> => ({
      ok: false,
      error: error instanceof Error ? error.message : String(error),
      authNeeded: false,
    }),
  );
}

/** Ask the user a yes/no question (the window's native confirm). */
export type Confirm = (question: string) => boolean;

function command(
  bridge: WelcomeBridge,
  confirm: Confirm,
  run: RunCommand,
): Promise<WelcomeResult<null>> {
  switch (run.kind) {
    case 'attach':
      return bridge.attach(run.host, run.instance);
    case 'resume': {
      // A resume continues the run's agents, which costs tokens: ask first.
      if (!confirm(`Resume run ${run.run} in ${run.project}? It continues the run's agents.`)) {
        return Promise.resolve({ok: true, value: null});
      }
      return bridge.resume(run.host, run.project, run.run);
    }
    case 'stop':
      return bridge.stop(run.host, run.instance, run.force);
    case 'retry':
      return bridge.retry();
    case 'show-run':
      return bridge.showRun();
    case 'show-welcome':
      return bridge.showWelcome();
  }
}

function hostStore(bridge: WelcomeBridge, firstHost: HostKey): Store<HostState, HostAction> {
  return createStore(hostStep, hostInitial(firstHost), (request, dispatch) => {
    switch (request.kind) {
      case 'host':
        void settled(bridge.host(request.host)).then(result =>
          dispatch({type: 'host-reply', token: request.token, result}),
        );
        return;
      case 'set-checkout':
        void settled(bridge.setCheckout(request.host, request.path)).then(result =>
          dispatch({type: 'checkout-reply', token: request.token, result}),
        );
        return;
      case 'sign-in':
        void settled(bridge.signIn(request.host)).then(result =>
          dispatch({type: 'sign-in-reply', token: request.token, result}),
        );
        return;
    }
  });
}

function startStore(bridge: WelcomeBridge): Store<StartState, StartAction> {
  return createStore(startStep, START_CLOSED, (request, dispatch) => {
    switch (request.kind) {
      case 'tasks':
        void settled(bridge.tasks(request.host, request.project)).then(result =>
          dispatch({type: 'tasks-reply', token: request.token, result}),
        );
        return;
      case 'start':
        void settled(
          bridge.start(request.host, request.project, request.task, request.args, request.token),
        ).then(result => dispatch({type: 'start-reply', token: request.token, result}));
        return;
      case 'cancel-start':
        // The start's own reply ends the cancelling state; this one carries nothing new.
        void settled(bridge.cancelStart(request.token));
        return;
    }
  });
}

/** Completion listings, each asked after a pause; a newer request for a field replaces it. */
function completionStore(bridge: WelcomeBridge): Store<CompletionState, CompletionAction> {
  const pending = new Map<CompletionField, ReturnType<typeof setTimeout>>();
  return createStore(completionStep, COMPLETION_EMPTY, (request, dispatch) => {
    clearTimeout(pending.get(request.field));
    pending.set(
      request.field,
      setTimeout(() => {
        pending.delete(request.field);
        void settled(bridge.complete(request.host, request.directory)).then(result =>
          dispatch({type: 'reply', field: request.field, token: request.token, result}),
        );
      }, request.afterMs),
    );
  });
}

/**
 * Build the stores over `bridge`, showing `firstHost`'s panel, and subscribe to its events.
 * `confirm` asks before a resume.
 */
export function welcomeStores(
  bridge: WelcomeBridge,
  confirm: Confirm,
  firstHost: HostKey,
): WelcomeStores {
  const welcome = createStore(welcomeStep, WELCOME_INITIAL, (request, dispatch) => {
    void settled(command(bridge, confirm, request.command)).then(result =>
      dispatch({type: 'reply', token: request.token, result}),
    );
  });
  const lists = createStore(listsStep, LISTS_EMPTY, () => {});
  const host = hostStore(bridge, firstHost);
  const start = startStore(bridge);
  const picker = createStore(pickerStep, PICKER_CLOSED, (request, dispatch) => {
    switch (request.kind) {
      case 'aliases':
        void settled(bridge.hosts()).then(result =>
          dispatch({type: 'aliases-reply', token: request.token, result}),
        );
        return;
      case 'show-host':
        host.dispatch({type: 'show', host: request.host});
        return;
    }
  });
  const completion = completionStore(bridge);
  bridge.onChrome(chrome => {
    welcome.dispatch({type: 'chrome', chrome});
    host.dispatch({type: 'chrome', chrome});
    if (chrome.mode === 'run') {
      start.dispatch({type: 'run-shown'});
      picker.dispatch({type: 'close'});
    }
  });
  bridge.onRecent(runs => lists.dispatch({type: 'runs', runs}));
  bridge.onListing(listing => lists.dispatch({type: 'listing', listing}));
  bridge.onShowHost(key => host.dispatch({type: 'show', host: key}));
  host.dispatch({type: 'show', host: firstHost});
  return {welcome, lists, host, start, picker, completion};
}
