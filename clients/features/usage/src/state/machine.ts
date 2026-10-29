/**
 * The Usage page's control state, as a machine (docs/contributing/clients-architecture.md,
 * "State"): it asks for the figures of a window, shows them, and decides what a late answer means.
 * Choosing another window leaves `loading` and enters it again, which stops the question being
 * asked, so an answer for a window the user left never arrives. The question is the operation
 * `load`, provided by `renderer/effects`; a deadline ends it when the helper never answers.
 */
import {unprovided} from '@vibesys/machine';
import {assign, setup} from 'xstate';
import {type UsageLoaded, type UsageView, usageView} from './usage-view.js';
import {spanOf, type UsageSpan, type UsageWindowChoice, type WindowClock} from './window.js';

/** What a question about the window came back with. */
export type UsageOutcome =
  | {readonly kind: 'usage'; readonly loaded: UsageLoaded}
  /** Every machine's VibeSys predates the question. */
  | {readonly kind: 'unsupported'};

export interface UsageContext {
  readonly window: UsageWindowChoice;
  readonly span: UsageSpan;
  /** The figures last read, for the window shown; null before any, or after the window changed. */
  readonly view: UsageView | null;
  /** Why the last question failed. */
  readonly problem: string | null;
}

export type UsageEvent =
  | {
      readonly type: 'choose';
      readonly window: UsageWindowChoice;
      readonly clock: WindowClock;
    }
  | {readonly type: 'refresh'; readonly clock: WindowClock};

export interface UsageInput {
  readonly window: UsageWindowChoice;
  readonly clock: WindowClock;
}

/** How long a question may take, in milliseconds. */
export const USAGE_DEADLINE_MS = 60_000;

export const usageMachine = setup({
  types: {
    context: {} as UsageContext,
    events: {} as UsageEvent,
    input: {} as UsageInput,
  },
  actors: {load: unprovided<UsageOutcome, {readonly span: UsageSpan}>('load')},
  actions: {
    ask: assign((_, params: {readonly window: UsageWindowChoice; readonly clock: WindowClock}) => ({
      window: params.window,
      span: spanOf(params.window, params.clock),
      problem: null,
    })),
    forget: assign({view: null}),
    show: assign((_, params: {readonly outcome: UsageOutcome}) => ({
      view: params.outcome.kind === 'usage' ? usageView(params.outcome.loaded) : null,
    })),
    fail: assign((_, params: {readonly problem: string}) => ({problem: params.problem})),
  },
  guards: {
    isUnsupported: (_, params: {readonly outcome: UsageOutcome}) =>
      params.outcome.kind === 'unsupported',
  },
  delays: {deadline: USAGE_DEADLINE_MS},
}).createMachine({
  id: 'usage',
  context: ({input}) => ({
    window: input.window,
    span: spanOf(input.window, input.clock),
    view: null,
    problem: null,
  }),
  initial: 'loading',
  states: {
    loading: {
      invoke: {
        id: 'load',
        src: 'load',
        input: ({context}) => ({span: context.span}),
        onDone: [
          {
            guard: {type: 'isUnsupported', params: ({event}) => ({outcome: event.output})},
            target: 'unsupported',
            actions: {type: 'show', params: ({event}) => ({outcome: event.output})},
          },
          {
            target: 'ready',
            actions: {type: 'show', params: ({event}) => ({outcome: event.output})},
          },
        ],
        onError: {
          target: 'failed',
          actions: {
            type: 'fail',
            params: ({event}) => ({
              problem: event.error instanceof Error ? event.error.message : String(event.error),
            }),
          },
        },
      },
      after: {
        deadline: {target: 'failed', actions: {type: 'fail', params: {problem: 'timed out'}}},
      },
    },
    ready: {},
    unsupported: {},
    failed: {},
  },
  on: {
    choose: {
      target: '.loading',
      reenter: true,
      actions: [
        {
          type: 'ask',
          params: ({event}) => ({window: event.window, clock: event.clock}),
        },
        'forget',
      ],
    },
    refresh: {
      target: '.loading',
      reenter: true,
      actions: {
        type: 'ask',
        params: ({context, event}) => ({window: context.window, clock: event.clock}),
      },
    },
  },
});
