/**
 * The time a stale value's age is read against. It ticks while some screen watches it, and has no
 * reading otherwise (an old one would show an age that is not the age), so a page with nothing
 * stale schedules nothing and a screen that starts to watch reads the time before it draws. The
 * reading is the viewer's clock, injected by the shell; the machine reads no clock of its own.
 */
import type {Instant} from '@vibesys/subscription';
import {assign, setup} from 'xstate';

/** How often the age is read again: it is shown in whole seconds. */
export const AGE_TICK_MS = 1_000;

interface Context {
  /** The viewer's time at the last reading, or null while nothing watches. */
  now: Instant | null;
  watchers: number;
}

export const clockMachine = setup({
  types: {
    context: {} as Context,
    events: {} as {type: 'watch'} | {type: 'unwatch'},
  },
  actions: {
    // Provided by the shell: the viewer's clock.
    read: assign({
      now: () => {
        throw new Error('the clock was not provided');
      },
    }),
    rest: assign({now: null}),
    join: assign({watchers: ({context}) => context.watchers + 1}),
    leave: assign({watchers: ({context}) => Math.max(0, context.watchers - 1)}),
  },
  guards: {alone: ({context}) => context.watchers <= 1},
  delays: {tick: AGE_TICK_MS},
}).createMachine({
  id: 'clock',
  context: {now: null, watchers: 0},
  initial: 'idle',
  states: {
    idle: {on: {watch: {target: 'ticking', actions: ['join', 'read']}}},
    ticking: {
      after: {tick: {target: 'ticking', reenter: true, actions: 'read'}},
      on: {
        watch: {actions: ['join', 'read']},
        unwatch: [{guard: 'alone', target: 'idle', actions: ['leave', 'rest']}, {actions: 'leave'}],
      },
    },
  },
});
