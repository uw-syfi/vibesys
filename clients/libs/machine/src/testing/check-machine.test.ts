import {describe, expect, test} from 'bun:test';
import {assign, setup, spawnChild} from 'xstate';
import {unprovided} from '../index.js';
import {checkMachine, type Live} from './index.js';

/** A load with a deadline: `ask` starts it, a reply or the deadline settles it. */
function loader(options: {readonly countOnly?: boolean; readonly leak?: boolean} = {}) {
  return setup({
    types: {context: {} as {readonly loads: number; readonly settled: number}},
    actors: {load: unprovided<number>('load')},
    actions: {
      leak: spawnChild('load', {id: 'leaked'}),
    },
  }).createMachine({
    id: 'loader',
    context: {loads: 0, settled: 0},
    initial: 'idle',
    states: {
      idle: {
        on: {
          ask: {
            target: 'loading',
            actions: [
              assign({loads: ({context}) => context.loads + 1}),
              ...(options.leak === true ? ['leak' as const] : []),
            ],
          },
        },
      },
      loading: {
        invoke: {
          id: 'load',
          src: 'load',
          onDone: {target: 'idle', actions: assign({settled: ({context}) => context.settled + 1})},
          onError: 'idle',
        },
        after: {1000: 'idle'},
        on: {cancel: 'idle'},
      },
    },
  });
}

function event(random: {pick<T>(items: readonly T[]): T; bool(): boolean}, live: Live) {
  const outcomes = [
    ...live.operations.map(operation => live.done(operation, 1)),
    ...live.delays.map(delay => live.fire(delay)),
  ];
  if (outcomes.length > 0 && random.bool()) return random.pick(outcomes);
  return random.pick([{type: 'ask'}, {type: 'cancel'}]);
}

describe('checkMachine', () => {
  test('passes a machine whose operations and delays live only in their state', () => {
    expect(() =>
      checkMachine({
        machine: loader(),
        input: undefined,
        event,
        invariants: snapshot =>
          snapshot.context.settled <= snapshot.context.loads ? [] : ['settled more than loaded'],
      }),
    ).not.toThrow();
  });

  test('fails a machine that starts an operation no state owns', () => {
    expect(() => checkMachine({machine: loader({leak: true}), input: undefined, event})).toThrow(
      /no explicit invoke id/,
    );
  });

  test('hands an invariant the work live beside the snapshot', () => {
    expect(() =>
      checkMachine({
        machine: loader(),
        input: undefined,
        event,
        // The state that invokes the load is the one that is loading, whatever the order of events.
        invariants: (snapshot, live) =>
          live.operations.includes('load') === snapshot.matches('loading')
            ? []
            : ['the load and its state disagree'],
      }),
    ).not.toThrow();
    expect(() =>
      checkMachine({
        machine: loader(),
        input: undefined,
        event,
        invariants: (snapshot, live) =>
          live.operations.length === 0 || snapshot.matches('idle') ? [] : ['loading'],
      }),
    ).toThrow(/checkMachine seed \d+, step \d+ \(ask\): loading/);
  });

  test('fails a machine whose own invariant breaks, naming the seed', () => {
    expect(() =>
      checkMachine({
        machine: loader(),
        input: undefined,
        event,
        invariants: snapshot => (snapshot.context.loads < 2 ? [] : ['loaded twice']),
      }),
    ).toThrow(/checkMachine seed \d+, step \d+ \(ask\): loaded twice/);
  });
});
