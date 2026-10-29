import {describe, expect, test} from 'bun:test';
import {checkMachine} from '@vibesys/machine/testing';
import {createActor, fromPromise, getInitialSnapshot, getNextSnapshot} from 'xstate';
import {type UsageOutcome, usageMachine} from './machine.js';
import type {UsageLoaded} from './usage-view.js';
import type {WindowClock} from './window.js';

const CLOCK: WindowClock = {now: 10_000_000_000, midnight: 9_990_000_000};
const LATER: WindowClock = {now: 10_000_060_000, midnight: 9_990_000_000};
const INPUT = {window: 'week', clock: CLOCK} as const;

const EMPTY: UsageLoaded = {
  sources: [],
  names: [],
  unreachable: 0,
  unsupported: 0,
};
const USAGE: UsageOutcome = {kind: 'usage', loaded: EMPTY};

function started() {
  return getInitialSnapshot(usageMachine, INPUT);
}

describe('usageMachine', () => {
  test('it asks for the window at once, and an answer shows its figures', () => {
    const asking = started();
    expect(asking.value).toBe('loading');
    expect(asking.context.span).toEqual({since: CLOCK.now - 7 * 24 * 3_600_000, until: CLOCK.now});
    expect(asking.context.view).toBeNull();
  });

  test('choosing a window asks again over that window and forgets the figures of the last', () => {
    const shown = getNextSnapshot(usageMachine, started(), {
      type: 'xstate.done.actor.load',
      output: USAGE,
    } as never);
    expect(shown.value).toBe('ready');
    expect(shown.context.view).not.toBeNull();
    const chosen = getNextSnapshot(usageMachine, shown, {
      type: 'choose',
      window: 'today',
      clock: LATER,
    });
    expect(chosen.value).toBe('loading');
    expect(chosen.context.window).toBe('today');
    expect(chosen.context.span).toEqual({since: CLOCK.midnight, until: LATER.now});
    expect(chosen.context.view).toBeNull();
  });

  test('a refresh asks again over the same window and keeps the figures until the new ones come', () => {
    const shown = getNextSnapshot(usageMachine, started(), {
      type: 'xstate.done.actor.load',
      output: USAGE,
    } as never);
    const again = getNextSnapshot(usageMachine, shown, {type: 'refresh', clock: LATER});
    expect(again.value).toBe('loading');
    expect(again.context.window).toBe('week');
    expect(again.context.span.until).toBe(LATER.now);
    expect(again.context.view).toBe(shown.context.view);
  });

  test('an answer that every machine cannot give is unsupported, a failure is a problem', () => {
    const unsupported = getNextSnapshot(usageMachine, started(), {
      type: 'xstate.done.actor.load',
      output: {kind: 'unsupported'},
    } as never);
    expect(unsupported.value).toBe('unsupported');
    expect(unsupported.context.view).toBeNull();
    const failed = getNextSnapshot(usageMachine, started(), {
      type: 'xstate.error.actor.load',
      error: new Error('host-a is unreachable'),
    } as never);
    expect({value: failed.value, problem: failed.context.problem}).toEqual({
      value: 'failed',
      problem: 'host-a is unreachable',
    });
  });

  test('the deadline gives up on a question nobody answers', () => {
    const timedOut = getNextSnapshot(usageMachine, started(), {
      type: 'xstate.after.deadline.usage.loading',
    } as never);
    expect({value: timedOut.value, problem: timedOut.context.problem}).toEqual({
      value: 'failed',
      problem: 'timed out',
    });
  });

  test('a late answer for a window the user left never shows', async () => {
    const answers: ((outcome: UsageOutcome) => void)[] = [];
    const actor = createActor(
      usageMachine.provide({
        actors: {
          load: fromPromise(() => new Promise<UsageOutcome>(resolve => answers.push(resolve))),
        },
      }),
      {input: INPUT},
    );
    actor.start();
    actor.send({type: 'choose', window: 'today', clock: LATER});
    expect(answers).toHaveLength(2);
    // The first question's answer comes after the second was asked.
    answers[0]?.({kind: 'unsupported'});
    await Promise.resolve();
    await Promise.resolve();
    expect(actor.getSnapshot().value).toBe('loading');
    answers[1]?.(USAGE);
    await Promise.resolve();
    await Promise.resolve();
    expect(actor.getSnapshot().value).toBe('ready');
    expect(actor.getSnapshot().context.window).toBe('today');
    actor.stop();
  });

  test('model: a question and its deadline live only while loading', () => {
    checkMachine({
      machine: usageMachine,
      input: INPUT,
      event: (random, live) =>
        random.pick([
          {
            type: 'choose' as const,
            window: random.pick(['today', 'week', 'fortnight'] as const),
            clock: LATER,
          },
          {type: 'refresh' as const, clock: LATER},
          ...live.operations.map(operation => live.done(operation, USAGE)),
          ...live.operations.map(operation => live.done(operation, {kind: 'unsupported'})),
          ...live.operations.map(operation => live.error(operation, new Error('failed'))),
          ...live.delays.map(delay => live.fire(delay)),
        ]),
      invariants: snapshot =>
        snapshot.value === 'ready' && snapshot.context.view === null
          ? ['ready without figures']
          : snapshot.value === 'unsupported' && snapshot.context.view !== null
            ? ['unsupported with figures']
            : [],
    });
  });
});
