import {describe, expect, test} from 'bun:test';
import type {StopOutcome} from './instances.js';
import {
  confirmText,
  IDLE,
  type StopEvent,
  type StopState,
  type StopStep,
  stopStep,
  stopView,
} from './stop-run.js';

/** A small seeded generator (fast-check is not a dependency; see the testing skill). */
function generator(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state * 1_664_525 + 1_013_904_223) >>> 0;
    return state / 2 ** 32;
  };
}

const OUTCOMES: readonly StopOutcome[] = [
  'stopped',
  'stopping',
  'not_running',
  'still_running',
  'unsupported',
];

function arbitraryEvent(random: () => number): StopEvent {
  switch (Math.floor(random() * 8)) {
    case 0:
      return {type: 'request', force: false};
    case 1:
      return {type: 'request', force: true};
    case 2:
      return {type: 'cancel'};
    case 3:
    case 4:
      return {type: 'confirm'};
    case 5:
      return {
        type: 'result',
        outcome: OUTCOMES[Math.floor(random() * OUTCOMES.length)] ?? 'stopped',
      };
    case 6:
      return {type: 'failed', message: 'boom'};
    default:
      return random() < 0.2 ? {type: 'ended'} : {type: 'confirm'};
  }
}

function run(events: readonly StopEvent[], from: StopState = IDLE): StopState {
  let state = from;
  for (const event of events) state = stopStep(state, event).state;
  return state;
}

/** What every step must satisfy, whatever came before. */
function checkStep(before: StopState, event: StopEvent, next: StopStep, accepted: boolean): void {
  if (before.phase === 'ended') expect(next.state).toBe(before);
  if (event.type === 'ended') expect(next.state.phase).toBe('ended');
  if (next.effect === null) return;
  // A stop command is sent only from a confirmed dialog, and never after an accepted stop.
  expect(before.phase).toBe('confirming');
  expect(event.type).toBe('confirm');
  expect(accepted).toBe(false);
  expect(next.effect.force).toBe(before.phase === 'confirming' && before.force);
}

describe('stop flow', () => {
  test('a graceful stop that is accepted waits for the run to end and never asks again', () => {
    const phases: string[] = [];
    let state: StopState = IDLE;
    let afterAccept: StopState = IDLE;
    const events: StopEvent[] = [
      {type: 'request', force: false},
      {type: 'confirm'},
      {type: 'result', outcome: 'stopping'},
      {type: 'request', force: false},
      {type: 'confirm'},
      {type: 'ended'},
    ];
    for (const event of events) {
      const next = stopStep(state, event);
      if (event.type === 'result') afterAccept = next.state;
      if (phases.length >= 3) expect(next.effect).toBeNull();
      state = next.state;
      phases.push(state.phase);
    }
    expect(phases).toEqual(['confirming', 'stopping', 'stopping', 'stopping', 'stopping', 'ended']);
    expect(stopView(afterAccept).text).toBe('Stopping…');
    expect(stopView(state)).toMatchObject({phase: 'ended', canForce: false});
  });

  test('cancelling the confirmation sends nothing and returns to idle', () => {
    const confirming = stopStep(IDLE, {type: 'request', force: false}).state;
    expect(stopStep(confirming, {type: 'cancel'})).toEqual({state: IDLE, effect: null});
  });

  test('unsupported and still_running offer a force stop, once', () => {
    for (const outcome of ['unsupported', 'still_running'] as const) {
      const failed = run([
        {type: 'request', force: false},
        {type: 'confirm'},
        {type: 'result', outcome},
      ]);
      expect(stopView(failed)).toMatchObject({phase: 'error', canForce: true});
      const confirming = stopStep(failed, {type: 'request', force: true}).state;
      const forced = stopStep(confirming, {type: 'confirm'});
      expect(forced.effect).toEqual({kind: 'stop', force: true});
      const again = stopStep(forced.state, {type: 'result', outcome});
      expect(stopView(again.state)).toMatchObject({phase: 'error', canForce: false});
    }
  });

  test('a force stop cannot be requested without a failed graceful stop', () => {
    expect(stopStep(IDLE, {type: 'request', force: true})).toEqual({state: IDLE, effect: null});
  });

  test('properties over generated event sequences', () => {
    const random = generator(20_261_010);
    for (let sequence = 0; sequence < 300; sequence += 1) {
      let state: StopState = IDLE;
      let accepted = false;
      for (let step = 0; step < 40; step += 1) {
        const event = arbitraryEvent(random);
        const next = stopStep(state, event);
        checkStep(state, event, next, accepted);
        state = next.state;
        accepted = accepted || (state.phase === 'stopping' && state.accepted);
      }
    }
  });

  test('every state reaches ended when the run ends', () => {
    const random = generator(7);
    for (let sequence = 0; sequence < 100; sequence += 1) {
      const events = Array.from({length: 12}, () => arbitraryEvent(random));
      expect(run([...events, {type: 'ended'}]).phase).toBe('ended');
    }
  });

  test('confirmation text names the run and host, and a force stop says it is stronger', () => {
    expect(confirmText({label: 'spsc', host: 'gpu-box', force: false})).toBe(
      'Stop run spsc on gpu-box? It stops at the next safe point and can be resumed.',
    );
    expect(confirmText({label: 'spsc', host: 'gpu-box', force: true})).toContain(
      'Force stop run spsc on gpu-box',
    );
  });
});
