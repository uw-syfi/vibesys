import {unprovided} from '@vibesys/machine';
import type {IntentState, PostIntentRequest, PostIntentResponse} from '@vibesys/runs/intents';
import {copyIntent, IntentStep} from '@vibesys/runs/intents';
import {assign, fromCallback, setup} from 'xstate';

export interface RunIntentsInput {
  readonly enabled: boolean;
}
export interface IntentEntry {
  readonly request: PostIntentRequest;
  readonly status: 'sending' | 'pending' | 'applied' | 'refused' | 'uncertain';
  readonly reason: string | null;
}
export interface RunIntentsContext {
  readonly enabled: boolean;
  readonly entries: readonly IntentEntry[];
  readonly current: PostIntentRequest | null;
  readonly error: string | null;
  readonly warning: string | null;
}
export type RunIntentsEvent =
  | {readonly type: 'send'; readonly request: PostIntentRequest}
  | {readonly type: 'retry'; readonly requestId: string}
  | {readonly type: 'outcomes'; readonly outcomes: readonly IntentState[]}
  | {readonly type: 'watchFailed'; readonly error: string};

function outcomeEntries(
  entries: readonly IntentEntry[],
  outcomes: readonly IntentState[],
): readonly IntentEntry[] {
  return entries.map(entry => {
    const outcome = outcomes.find(item => item.controlId === entry.request.requestId);
    if (!outcome) return entry;
    if (outcome.step !== IntentStep.APPLIED && outcome.step !== IntentStep.REFUSED) return entry;
    return {
      ...entry,
      status: outcome.step === IntentStep.APPLIED ? 'applied' : 'refused',
      reason: outcome.reason ?? null,
    };
  });
}

export const runIntentsMachine = setup({
  types: {
    context: {} as RunIntentsContext,
    events: {} as RunIntentsEvent,
    input: {} as RunIntentsInput,
  },
  actors: {
    post: unprovided<PostIntentResponse, PostIntentRequest>('post'),
    watch: fromCallback<RunIntentsEvent>(() => {
      throw new Error('watch was not provided');
    }),
  },
  actions: {
    fail: assign(({context}, params: {readonly error: string}) => ({
      entries: context.entries.map(entry =>
        entry.status === 'sending'
          ? {...entry, status: 'uncertain' as const, reason: params.error}
          : entry,
      ),
      error: params.error,
    })),
    apply: assign(({context, event}) =>
      event.type === 'outcomes' ? {entries: outcomeEntries(context.entries, event.outcomes)} : {},
    ),
  },
  delays: {deadline: 35_000},
}).createMachine({
  id: 'runIntents',
  context: ({input}) => ({
    enabled: input.enabled,
    entries: [],
    current: null,
    error: null,
    warning: null,
  }),
  initial: 'routing',
  states: {
    routing: {
      always: [{guard: ({context}) => context.enabled, target: 'active'}, {target: 'disabled'}],
    },
    disabled: {},
    active: {
      invoke: {
        id: 'watch',
        src: 'watch',
        onError: {actions: assign(({event}) => ({error: String(event.error)}))},
      },
      on: {
        outcomes: {actions: 'apply'},
        watchFailed: {actions: assign(({event}) => ({error: event.error}))},
      },
      initial: 'idle',
      states: {
        idle: {
          on: {
            send: {
              guard: ({context, event}) =>
                !context.entries.some(entry => entry.request.requestId === event.request.requestId),
              target: 'sending',
              actions: assign(({context, event}) => {
                const request = copyIntent(event.request);
                return {
                  current: request,
                  error: null,
                  entries: [
                    ...context.entries,
                    {request, status: 'sending' as const, reason: null},
                  ],
                };
              }),
            },
            retry: {
              guard: ({context, event}) =>
                context.entries.some(
                  entry =>
                    entry.request.requestId === event.requestId && entry.status === 'uncertain',
                ),
              target: 'sending',
              actions: assign(({context, event}) => ({
                current:
                  context.entries.find(entry => entry.request.requestId === event.requestId)
                    ?.request ?? null,
                error: null,
                entries: context.entries.map(entry =>
                  entry.request.requestId === event.requestId
                    ? {...entry, status: 'sending' as const, reason: null}
                    : entry,
                ),
              })),
            },
          },
        },
        sending: {
          invoke: {
            id: 'post',
            src: 'post',
            input: ({context}) => {
              if (!context.current) throw new Error('no intent to post');
              return context.current;
            },
            onDone: {
              target: 'idle',
              actions: assign(({context, event}) => ({
                warning: event.output.orderedReader
                  ? null
                  : 'Posting order has not been confirmed. Start or restart this run with the current VibeSys.',
                entries: context.entries.map(entry =>
                  entry.status === 'sending' ? {...entry, status: 'pending' as const} : entry,
                ),
              })),
            },
            onError: {
              target: 'idle',
              actions: {type: 'fail', params: ({event}) => ({error: String(event.error)})},
            },
          },
          after: {
            deadline: {
              target: 'idle',
              actions: {
                type: 'fail',
                params: {error: 'No admission reply. Retry uses the same request id.'},
              },
            },
          },
        },
      },
    },
  },
});
