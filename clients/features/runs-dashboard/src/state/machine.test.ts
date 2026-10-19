import {describe, expect, test} from 'bun:test';
import {checkMachine} from '@vibesys/machine/testing';
import {getInitialSnapshot, getNextSnapshot} from 'xstate';
import {runsDashboardMachine} from './machine.js';

describe('runsDashboardMachine', () => {
  test('start begins the work, and the deadline gives up on it', () => {
    const working = getNextSnapshot(runsDashboardMachine, getInitialSnapshot(runsDashboardMachine), {type: 'start'});
    expect(working.value).toBe('working');
    const timedOut = getNextSnapshot(runsDashboardMachine, working, {
      type: 'xstate.after.deadline.runsDashboard.working',
    } as never);
    expect({value: timedOut.value, error: timedOut.context.error}).toEqual({
      value: 'idle',
      error: 'timed out',
    });
  });

  test('model: work and its deadline live only while working; attempts are bounded', () => {
    checkMachine({
      machine: runsDashboardMachine,
      input: undefined,
      event: (random, live) =>
        random.pick([
          {type: 'start'},
          {type: 'cancel'},
          ...live.operations.map(operation => live.done(operation, null)),
          ...live.operations.map(operation => live.error(operation, new Error('failed'))),
          ...live.delays.map(delay => live.fire(delay)),
        ]),
      invariants: snapshot => (snapshot.context.attempts <= 3 ? [] : ['more than three attempts']),
    });
  });
});
