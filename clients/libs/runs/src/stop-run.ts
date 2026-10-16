/**
 * Stopping a run, as a pure state machine: what the title strip and the run lists show and allow
 * between the click on "Stop run" and the run's end.
 *
 *   idle -> confirming -> stopping -> ended | error
 *
 * `stopping` has two stages: the stop command has been sent (`accepted: false`), and the server
 * accepted it and exits on its own at its next safe point (`accepted: true`, the host's
 * `stopping` outcome: do not ask again, wait for the run to end, unless a view offers a force stop
 * because the stop has hung past its bound). `error` carries the text to show
 * and whether a force stop is on offer. The functions here decide; `StopController` performs the
 * effects and feeds results back.
 */
import type {StopOutcome} from '@vibesys/contracts/host-helper';
import type {HostKey} from '@vibesys/hosts';

/** Key of a run's stop flow in `ChromeState.stops`. */
export function stopKey(host: HostKey, instanceId: string): string {
  return `${host}\n${instanceId}`;
}

export type StopState =
  | {readonly phase: 'idle'}
  /** `accepted`: a force stop asked for while an accepted graceful stop is still ending. */
  | {readonly phase: 'confirming'; readonly force: boolean; readonly accepted: boolean}
  | {readonly phase: 'stopping'; readonly force: boolean; readonly accepted: boolean}
  | {readonly phase: 'ended'}
  | {readonly phase: 'error'; readonly message: string; readonly canForce: boolean};

export type StopEvent =
  /** The user asked to stop (`force`: to force-stop after a stop failed). */
  | {readonly type: 'request'; readonly force: boolean}
  | {readonly type: 'cancel'}
  /** The owner withdrew this stop before its command reached the host. */
  | {readonly type: 'abandon'}
  | {readonly type: 'confirm'}
  /** The host reported what `instances stop` observed. */
  | {readonly type: 'result'; readonly outcome: StopOutcome}
  /** The stop command itself could not run. */
  | {readonly type: 'failed'; readonly message: string}
  /** The run is no longer live (stopped by anyone). */
  | {readonly type: 'ended'};

/** What the shell must do next, besides showing the new state. */
type StopEffect =
  | {readonly kind: 'confirm'; readonly force: boolean}
  | {readonly kind: 'stop'; readonly force: boolean}
  | null;

export const STOP_IDLE: StopState = {phase: 'idle'};

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

/** The one request a transition from `before` to `after` authorizes. */
function effectOf(before: StopState, after: StopState): StopEffect {
  if (after.phase === 'confirming' && before.phase !== 'confirming') {
    return {kind: 'confirm', force: after.force};
  }
  // Declining a force stop returns to an accepted stop, which sends nothing.
  return before.phase === 'confirming' && after.phase === 'stopping' && !after.accepted
    ? {kind: 'stop', force: before.force}
    : null;
}

function transition(state: StopState, event: StopEvent): StopState {
  switch (state.phase) {
    case 'idle':
      return event.type === 'request' && !event.force
        ? {phase: 'confirming', force: false, accepted: false}
        : state;
    case 'error':
      return event.type === 'request' && (!event.force || state.canForce)
        ? {phase: 'confirming', force: event.force, accepted: false}
        : state;
    case 'confirming':
      return afterQuestion(state, event);
    case 'stopping':
      return state.accepted ? afterAccepted(state, event) : afterCommand(state, event);
    case 'ended':
      return state;
  }
}

/** The user answered the stop question, or its owner withdrew it. */
function afterQuestion(state: StopState, event: StopEvent): StopState {
  if (state.phase !== 'confirming') return state;
  if (event.type === 'cancel' || event.type === 'abandon') {
    // Declining a force stop leaves the accepted graceful stop ending, as it was.
    return state.accepted ? {phase: 'stopping', force: false, accepted: true} : STOP_IDLE;
  }
  return event.type === 'confirm'
    ? {phase: 'stopping', force: state.force, accepted: false}
    : state;
}

/**
 * A graceful stop the server accepted and has not finished: the one thing that may still happen
 * here is a force stop, which a view offers once the stop has hung past its bound (the Run screen
 * times that on its injected clock). A forced stop is not forced again.
 */
function afterAccepted(state: StopState, event: StopEvent): StopState {
  if (state.phase !== 'stopping' || state.force) return state;
  return event.type === 'request' && event.force
    ? {phase: 'confirming', force: true, accepted: true}
    : state;
}

/** A stop command that is in flight: it fails, or the host reports what it observed. */
function afterCommand(state: StopState, event: StopEvent): StopState {
  if (state.phase !== 'stopping') return state;
  if (event.type === 'abandon') return STOP_IDLE;
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
