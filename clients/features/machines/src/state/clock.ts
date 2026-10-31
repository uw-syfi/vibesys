/**
 * The time a stale value's age is read against. It ticks while some screen watches it, and holds
 * its last reading otherwise, so a page with nothing stale schedules nothing. The reading is the
 * viewer's clock, injected by the shell; the machine reads no clock of its own.
 */
import type {Instant} from '@vibesys/subscription';
import {assign, setup} from 'xstate';

/** How often the age is read again: it is shown in whole seconds. */
export const AGE_TICK_MS = 1_000;

interface Context {
  now: Instant;
  watchers: number;
}

export const clockMachine = setup({
  types: {
    context: {} as Context,
    input: {} as {readonly now: Instant},
    events: {} as {type: 'watch'} | {type: 'unwatch'},
  },
  actions: {
    // Provided by the shell: the viewer's clock.
    read: assign({
      now: () => {
        throw new Error('the clock was not provided');
      },
    }),
    join: assign({watchers: ({context}) => context.watchers + 1}),
    leave: assign({watchers: ({context}) => Math.max(0, context.watchers - 1)}),
  },
  guards: {alone: ({context}) => context.watchers <= 1},
  delays: {tick: AGE_TICK_MS},
}).createMachine({
  id: 'clock',
  context: ({input}) => ({now: input.now, watchers: 0}),
  initial: 'idle',
  states: {
    idle: {on: {watch: {target: 'ticking', actions: ['join', 'read']}}},
    ticking: {
      after: {tick: {target: 'ticking', reenter: true, actions: 'read'}},
      on: {
        watch: {actions: ['join', 'read']},
        unwatch: [{guard: 'alone', target: 'idle', actions: 'leave'}, {actions: 'leave'}],
      },
    },
  },
});
