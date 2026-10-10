/**
 * Stopping a run, as a pure state machine: what the title strip and the run lists show and allow
 * between the click on "Stop run" and the run's end.
 *
 *   idle -> confirming -> stopping -> ended | error
 *
 * `stopping` has two stages: the stop command has been sent (`accepted: false`), and the server
 * accepted it and exits on its own at its next safe point (`accepted: true`, the host's
 * `stopping` outcome: do not ask again, wait for the run to end). `error` carries the text to show
 * and whether a force stop is on offer. The functions here decide; `StopController` performs the
 * effects and feeds results back.
 */
import type {HostKey} from './host-settings.js';
import type {StopOutcome} from './instances.js';

/** Key of a run's stop flow in `ChromeState.stops`. */
export function stopKey(host: HostKey, instanceId: string): string {
  return `${host}\n${instanceId}`;
}

export type StopState =
  | {readonly phase: 'idle'}
  | {readonly phase: 'confirming'; readonly force: boolean}
  | {readonly phase: 'stopping'; readonly force: boolean; readonly accepted: boolean}
  | {readonly phase: 'ended'}
  | {readonly phase: 'error'; readonly message: string; readonly canForce: boolean};

export type StopEvent =
  /** The user asked to stop (`force`: to force-stop after a stop failed). */
  | {readonly type: 'request'; readonly force: boolean}
  | {readonly type: 'cancel'}
  | {readonly type: 'confirm'}
  /** The host reported what `instances stop` observed. */
  | {readonly type: 'result'; readonly outcome: StopOutcome}
  /** The stop command itself could not run. */
  | {readonly type: 'failed'; readonly message: string}
  /** The run is no longer live (stopped by anyone). */
  | {readonly type: 'ended'};

/** What to do next, besides showing the new state. */
type StopEffect = {readonly kind: 'stop'; readonly force: boolean} | null;

export const IDLE: StopState = {phase: 'idle'};

export interface StopStep {
  readonly state: StopState;
  readonly effect: StopEffect;
}

export function stopStep(state: StopState, event: StopEvent): StopStep {
  if (state.phase === 'ended') return {state, effect: null};
  if (event.type === 'ended') return {state: {phase: 'ended'}, effect: null};
  const next = transition(state, event);
  return {state: next, effect: next === state ? null : effectOf(state, next)};
}

/** The stop command a transition from `before` to `after` sends: only a confirmed dialog does. */
function effectOf(before: StopState, after: StopState): StopEffect {
  return before.phase === 'confirming' && after.phase === 'stopping'
    ? {kind: 'stop', force: before.force}
    : null;
}

function transition(state: StopState, event: StopEvent): StopState {
  switch (state.phase) {
    case 'idle':
      return event.type === 'request' && !event.force ? {phase: 'confirming', force: false} : state;
    case 'error':
      return event.type === 'request' && (!event.force || state.canForce)
        ? {phase: 'confirming', force: event.force}
        : state;
    case 'confirming':
      if (event.type === 'cancel') return IDLE;
      return event.type === 'confirm'
        ? {phase: 'stopping', force: state.force, accepted: false}
        : state;
    case 'stopping':
      return state.accepted ? state : afterCommand(state, event);
    case 'ended':
      return state;
  }
}

/** A stop command that is in flight: it fails, or the host reports what it observed. */
function afterCommand(state: StopState, event: StopEvent): StopState {
  if (state.phase !== 'stopping') return state;
  if (event.type === 'failed') return {phase: 'error', message: event.message, canForce: false};
  return event.type === 'result' ? afterOutcome(event.outcome, state.force) : state;
}

function afterOutcome(outcome: StopOutcome, force: boolean): StopState {
  switch (outcome) {
    case 'stopped':
    case 'not_running':
      return {phase: 'ended'};
    case 'stopping':
      return {phase: 'stopping', force, accepted: true};
    case 'still_running':
      return {
        phase: 'error',
        message: 'The run did not stop and is still running.',
        canForce: !force,
      };
    case 'unsupported':
      return {
        phase: 'error',
        message: 'This run cannot be stopped gracefully from here.',
        canForce: !force,
      };
  }
}

/** The question asked before a stop; a force stop gets its own, stronger one. */
export function confirmText(run: {
  readonly label: string;
  readonly host: string;
  readonly force: boolean;
}): string {
  return run.force
    ? `Force stop run ${run.label} on ${run.host}? It ends right away without waiting for a safe point, so work since the last one is lost. It can still be resumed.`
    : `Stop run ${run.label} on ${run.host}? It stops at the next safe point and can be resumed.`;
}

/** What a page shows for a run's stop flow. */
export interface StopView {
  readonly phase: StopState['phase'];
  /** One line for the run's row or the title strip; empty when there is nothing to say. */
  readonly text: string;
  /** True when "Force stop" is on offer. */
  readonly canForce: boolean;
}

export function stopView(state: StopState): StopView {
  switch (state.phase) {
    case 'idle':
    case 'confirming':
      return {phase: state.phase, text: '', canForce: false};
    case 'stopping':
      return {phase: 'stopping', text: 'Stopping…', canForce: false};
    case 'ended':
      return {phase: 'ended', text: 'Run ended', canForce: false};
    case 'error':
      return {phase: 'error', text: state.message, canForce: state.canForce};
  }
}
