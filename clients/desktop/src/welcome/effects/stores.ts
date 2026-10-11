/**
 * The welcome view's shell: one store per screen, each request performed through the welcome
 * bridge and its reply dispatched back with the request's token, and the main process's events
 * fanned out to the stores that fold them. This is the only module of the view that talks to the
 * bridge.
 */
import type {HostKey} from '../../host-settings.js';
import type {WelcomeBridge, WelcomeResult} from '../../welcome-protocol.js';
import {type HostAction, type HostState, hostInitial, hostStep} from '../state/host.js';
import {PICKER_CLOSED, type PickerAction, type PickerState, pickerStep} from '../state/picker.js';
import {RECENT_EMPTY, type RecentAction, type RecentState, recentStep} from '../state/recent.js';
import {START_CLOSED, type StartAction, type StartState, startStep} from '../state/start.js';
import {
  type RunCommand,
  WELCOME_INITIAL,
  type WelcomeAction,
  type WelcomeState,
  welcomeStep,
} from '../state/welcome.js';
import {createStore, type Store} from './store.js';

/** How often the lists look again while a stop is ending. */
const STOP_REFRESH_MS = 3_000;

export interface WelcomeStores {
  readonly welcome: Store<WelcomeState, WelcomeAction>;
  readonly recent: Store<RecentState, RecentAction>;
  readonly host: Store<HostState, HostAction>;
  readonly start: Store<StartState, StartAction>;
  readonly picker: Store<PickerState, PickerAction>;
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
  const recent = createStore(recentStep, RECENT_EMPTY, (request, dispatch) => {
    void settled(bridge.recentStatus(request.host)).then(result =>
      dispatch({type: 'status-reply', host: request.host, token: request.token, result}),
    );
  });
  const host = createStore(hostStep, hostInitial(firstHost), (request, dispatch) => {
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
  const start = createStore(startStep, START_CLOSED, (request, dispatch) => {
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
  // While a stop the host accepted is ending, the lists look again every few seconds (the only
  // refresh request; phase 5's `instances.watch` replaces it).
  let lookAgain: ReturnType<typeof setTimeout> | undefined;
  const watchStops = (): void => {
    const ending = Object.values(welcome.getState().chrome.stops).some(
      view => view.phase === 'stopping',
    );
    if (!ending || lookAgain !== undefined) return;
    lookAgain = setTimeout(() => {
      lookAgain = undefined;
      recent.dispatch({type: 'tick'});
      host.dispatch({type: 'tick'});
      watchStops();
    }, STOP_REFRESH_MS);
  };
  bridge.onChrome(chrome => {
    welcome.dispatch({type: 'chrome', chrome});
    watchStops();
    recent.dispatch({type: 'chrome', chrome});
    host.dispatch({type: 'chrome', chrome});
    if (chrome.mode === 'run') {
      start.dispatch({type: 'run-shown'});
      picker.dispatch({type: 'close'});
    }
  });
  bridge.onRecent(runs => recent.dispatch({type: 'runs', runs}));
  bridge.onShowHost(key => host.dispatch({type: 'show', host: key}));
  host.dispatch({type: 'show', host: firstHost});
  return {welcome, recent, host, start, picker};
}
