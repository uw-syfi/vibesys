import {describe, expect, test} from 'bun:test';
import {createActor, setup} from 'xstate';
import {actorStore, clockFrom, type ScheduleTimeout} from './index.js';

/** A scheduler the test advances by hand: nothing runs until `advance`. */
class ManualClock {
  #now = 0;
  readonly #pending = new Map<number, {readonly at: number; readonly run: () => void}>();
  #next = 1;

  readonly schedule: ScheduleTimeout = (run, delayMs) => {
    const id = this.#next;
    this.#next += 1;
    this.#pending.set(id, {at: this.#now + delayMs, run});
    return () => this.#pending.delete(id);
  };

  get pending(): number {
    return this.#pending.size;
  }

  advance(ms: number): void {
    this.#now += ms;
    for (const [id, timer] of [...this.#pending].sort(([, a], [, b]) => a.at - b.at)) {
      if (timer.at > this.#now) continue;
      this.#pending.delete(id);
      timer.run();
    }
  }
}

const notice = setup({types: {context: {} as {readonly shown: number}}}).createMachine({
  id: 'notice',
  context: {shown: 0},
  initial: 'hidden',
  states: {
    hidden: {on: {show: 'shown'}},
    shown: {after: {500: 'hidden'}, on: {dismiss: 'hidden'}},
  },
});

describe('clockFrom', () => {
  test('runs a delay on the injected clock and cancels it when the state is left', () => {
    const clock = new ManualClock();
    const actor = createActor(notice, {clock: clockFrom(clock.schedule)}).start();
    actor.send({type: 'show'});
    expect(clock.pending).toBe(1);
    clock.advance(499);
    expect(actor.getSnapshot().value).toBe('shown');
    clock.advance(1);
    expect(actor.getSnapshot().value).toBe('hidden');

    actor.send({type: 'show'});
    actor.send({type: 'dismiss'});
    expect(clock.pending).toBe(0);
  });
});

describe('actorStore', () => {
  test('notifies only when the view changes, and dispatches events to the actor', () => {
    const actor = createActor(notice, {clock: clockFrom(new ManualClock().schedule)}).start();
    const store = actorStore(actor, snapshot => snapshot.value);
    let heard = 0;
    store.subscribe(() => {
      heard += 1;
    });
    store.dispatch({type: 'dismiss'});
    expect(heard).toBe(0);
    store.dispatch({type: 'show'});
    expect({value: store.getState(), heard}).toEqual({value: 'shown', heard: 1});
  });

  test('snapshots subscriptions while publishing a changed view', () => {
    const actor = createActor(notice, {clock: clockFrom(new ManualClock().schedule)}).start();
    const store = actorStore(actor, snapshot => snapshot.value);
    const trace: string[] = [];
    const added = () => trace.push('added');
    let changed = false;
    let unsubscribeRemoved: () => void = () => undefined;
    store.subscribe(() => {
      trace.push('first');
      if (changed) return;
      changed = true;
      unsubscribeRemoved();
      store.subscribe(added);
    });
    unsubscribeRemoved = store.subscribe(() => trace.push('removed'));

    store.dispatch({type: 'show'});
    store.dispatch({type: 'dismiss'});

    expect(trace).toEqual(['first', 'removed', 'first', 'added']);
  });
});
