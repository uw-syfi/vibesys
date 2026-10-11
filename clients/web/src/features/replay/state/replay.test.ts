import {describe, expect, test} from 'bun:test';
import {event} from '@vibesys/backend-client/testing';
import {checkModel} from '@vibesys/core-state/testing';
import {
  DEFAULT_REPLAY_FIXTURE_URL,
  REPLAY_IDLE,
  type ReplayAction,
  replayFailure,
  replayStep,
} from './replay.js';

const EVENTS = [event(1, 'agent_output_chunk', 'hello')];

describe('replayStep', () => {
  test('awaits only issued loads and never folds a superseded one', () => {
    checkModel({
      initial: REPLAY_IDLE,
      step: replayStep,
      action: (random, issued): ReplayAction =>
        random.int(3) === 0
          ? {type: 'load'}
          : {
              type: 'reply',
              token: random.pick(issued),
              result: random.bool() ? {ok: true, events: EVENTS} : {ok: false, error: 'HTTP 503'},
            },
      issues: request => (request.kind === 'fetch' ? request.token : null),
      awaited: state => ({load: state.kind === 'loading' ? state.token : null}),
      reply: action => (action.type === 'reply' ? {slot: 'load', token: action.token} : null),
      also: (before, action, after, requests) => {
        // Events fold at most once: only the awaited load's success appends them.
        const appends = requests.filter(request => request.kind === 'append');
        if (appends.length > 0) {
          expect(before.kind).toBe('loading');
          expect(after.kind).toBe('loaded');
        }
        // A loaded replay never loads again; a failed one loads again on Retry.
        if (action.type === 'load' && before.kind === 'loaded') expect(requests).toEqual([]);
        if (action.type === 'load' && before.kind === 'failed') expect(after.kind).toBe('loading');
      },
    });
  });

  test('a failed load shows its reason and Retry asks for a fresh attempt', () => {
    const first = replayStep(REPLAY_IDLE, {type: 'load'});
    expect(first.requests).toEqual([
      {kind: 'fetch', token: 0, url: `${DEFAULT_REPLAY_FIXTURE_URL}?attempt=0`},
    ]);
    const failed = replayStep(first.state, {
      type: 'reply',
      token: 0,
      result: {ok: false, error: 'Replay fixture request failed with 503'},
    }).state;
    expect(replayFailure(failed)).toBe('Replay fixture request failed with 503');
    const retry = replayStep(failed, {type: 'load'});
    expect(replayFailure(retry.state)).toBeNull();
    expect(retry.requests).toEqual([
      {kind: 'fetch', token: 1, url: `${DEFAULT_REPLAY_FIXTURE_URL}?attempt=1`},
    ]);
  });
});
