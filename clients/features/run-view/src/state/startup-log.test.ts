import {describe, expect, test} from 'bun:test';
import type {LogStream} from '@vibesys/contracts/host-helper';
import type {LogListOutcome} from '@vibesys/logs';
import {checkMachine} from '@vibesys/machine/testing';
import {type CoreState, initialCoreState} from '@vibesys/run-state';

type RoundState = CoreState['rounds'][number];

import {
  shownStream,
  startupLogMachine,
  startupLogNotice,
  startupLogObservation,
  startupLogView,
} from './startup-log.js';

function run(
  status: CoreState['status'],
  stage: 'building_image' | 'starting_container' | null,
  rounds: readonly RoundState[] = [],
): CoreState {
  const startup =
    stage === null
      ? null
      : {
          stage,
          progress: null,
          stageSince: '2026-10-15T00:00:00Z',
          lastActivityAt: '2026-10-15T00:00:00Z',
        };
  return {...initialCoreState(), status, startup, rounds};
}

describe('startupLogObservation', () => {
  test('shows while starting, and after a failure while starting', () => {
    expect(startupLogObservation(run('starting', null))).toEqual({visible: true, stage: null});
    expect(startupLogObservation(run('starting', 'building_image'))).toEqual({
      visible: true,
      stage: 'building_image',
    });
    expect(startupLogObservation(run('failed', 'starting_container')).visible).toBe(true);
  });

  test('hides once the run is ready, and after a failure that was not a startup failure', () => {
    expect(startupLogObservation(run('running', 'starting_container')).visible).toBe(false);
    expect(startupLogObservation(run('failed', null)).visible).toBe(false);
    const round = {} as RoundState;
    expect(startupLogObservation(run('failed', 'starting_container', [round])).visible).toBe(false);
  });
});

const STREAMS: readonly LogStream[] = [
  {
    id: '000001',
    owner: {kind: 'startup_step', stage: 'building_image'},
    command: 'b',
    retained_start: 0,
    size: 0,
    outcome: 'ended',
  },
  {
    id: '000002',
    owner: {kind: 'run_narration'},
    command: 'n',
    retained_start: 0,
    size: 0,
    outcome: null,
  },
  {
    id: '000003',
    owner: {kind: 'startup_step', stage: 'starting_container'},
    command: 'c',
    retained_start: 0,
    size: 0,
    outcome: null,
  },
];

const OUTCOMES: readonly LogListOutcome[] = [
  {ok: true, instanceId: '0123456789ab', streams: STREAMS},
  {ok: true, instanceId: '0123456789ab', streams: STREAMS.slice(0, 1)},
  {ok: true, instanceId: '0123456789ab', streams: []},
  {ok: false, failure: {code: 'transport', message: 'dropped'}},
  {ok: false, failure: {code: 'feature_not_negotiated', message: 'no logs'}},
];

describe('startupLogMachine', () => {
  test('model: work runs only while expanded, and the follower reads the stream on screen', () => {
    checkMachine({
      machine: startupLogMachine,
      input: {retryMs: 1000},
      event: (random, live) => {
        const settle = live.operations
          .filter(operation => operation === 'list')
          .flatMap(operation => [
            live.done(operation, random.pick(OUTCOMES)),
            live.error(operation, new Error('bridge failed')),
          ]);
        const fire = live.delays.map(delay => live.fire(delay));
        return random.pick([
          {
            type: 'observe',
            visible: random.bool(),
            stage: random.pick(['building_image', 'starting_container', null]),
          },
          {type: 'expand'},
          {type: 'collapse'},
          {type: 'select', streamId: random.pick(['000001', '000003', '999999'])},
          ...settle,
          ...settle,
          ...fire,
        ]);
      },
      invariants: snapshot => {
        const problems: string[] = [];
        const view = startupLogView(snapshot, null);
        const following = snapshot.matches({shown: {expanded: {follow: 'following'}}});
        if (following && !view.expanded) problems.push('a follower runs while collapsed');
        if (following && snapshot.context.followed !== shownStream(snapshot.context))
          problems.push('the follower reads a stream that is not on screen');
        if (view.streams.some(stream => stream.id === '000002'))
          problems.push('a stream that is not startup output is listed');
        return problems;
      },
    });
  });

  test('the notice says unsupported instead of an error', () => {
    const view = {
      visible: true,
      expanded: true,
      listing: 'unsupported' as const,
      listFailure: {code: 'feature_not_negotiated' as const, message: 'no logs'},
      streams: [],
      shown: null,
      lines: [],
      earlier: false,
      readFailure: null,
    };
    expect(startupLogNotice(view)).toContain('does not serve logs');
  });
});
