import {unprovided} from '@vibesys/machine';
import type {IntentOutcome, PostIntentRequest, PostIntentResponse} from '@vibesys/runs/intents';
import {
  copyIntent,
  IntentAction,
  IntentRefused,
  intentAdmissionWarning,
} from '@vibesys/runs/intents';
import {assign, fromCallback, setup} from 'xstate';
import {foldOutcomes, type OutcomeRow} from './history.js';

export interface RunIntentsInput {
  readonly enabled: boolean;
}
export interface IntentEntry {
  readonly request: PostIntentRequest;
  readonly status: 'sending' | 'pending' | 'applied' | 'refused' | 'uncertain';
  readonly reason: string | null;
}
export interface IntentRow {
  readonly id: string;
  readonly source: 'intent' | 'epoch-started';
  readonly action: IntentAction;
  readonly status: IntentEntry['status'];
  readonly reason: string | null;
  readonly epoch: bigint | null;
  readonly current: boolean;
}
export interface RunIntentsContext {
  readonly enabled: boolean;
  readonly entries: readonly IntentEntry[];
  readonly outcomes: readonly OutcomeRow[];
  readonly current: PostIntentRequest | null;
  readonly error: string | null;
  readonly warning: string | null;
}
export type RunIntentsEvent =
  | {readonly type: 'send'; readonly request: PostIntentRequest}
  | {readonly type: 'retry'; readonly requestId: string}
  | {readonly type: 'outcomes'; readonly outcomes: readonly IntentOutcome[]}
  | {readonly type: 'watchFailed'; readonly error: string};

function outcomeEntries(
  entries: readonly IntentEntry[],
  outcomes: readonly OutcomeRow[],
): readonly IntentEntry[] {
  return entries.map(entry => {
    const outcome = outcomes.find(
      item => item.source === 'intent' && item.id === entry.request.requestId,
    );
    if (!outcome) return entry;
    return {
      ...entry,
      status: outcome.status,
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
    fail: assign(
      ({context}, params: {readonly error: string; readonly status: 'refused' | 'uncertain'}) => ({
        entries: context.entries.map(entry =>
          entry.status === 'sending'
            ? {...entry, status: params.status, reason: params.error}
            : entry,
        ),
        error: params.status === 'uncertain' ? params.error : context.error,
      }),
    ),
    apply: assign(({context, event}) => {
      if (event.type !== 'outcomes') return {};
      const outcomes = foldOutcomes(context.outcomes, event.outcomes);
      const entries = outcomeEntries(context.entries, outcomes);
      return {
        entries,
        outcomes,
        error: entries.find(entry => entry.status === 'uncertain')?.reason ?? null,
        warning: entries.every(entry => entry.status === 'applied' || entry.status === 'refused')
          ? null
          : context.warning,
      };
    }),
  },
  delays: {deadline: 35_000},
}).createMachine({
  id: 'runIntents',
  context: ({input}) => ({
    enabled: input.enabled,
    entries: [],
    outcomes: [],
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
              actions: assign(({context, event}) => {
                const entries = context.entries.map(entry =>
                  entry.status === 'sending' ? {...entry, status: 'pending' as const} : entry,
                );
                const pending =
                  entries.find(entry => entry.request.requestId === event.output.requestId)
                    ?.status === 'pending';
                return {entries, warning: pending ? intentAdmissionWarning(event.output) : null};
              }),
            },
            onError: {
              target: 'idle',
              actions: {
                type: 'fail',
                params: ({event}) => ({
                  error:
                    event.error instanceof IntentRefused
                      ? event.error.message
                      : String(event.error),
                  status: event.error instanceof IntentRefused ? 'refused' : 'uncertain',
                }),
              },
            },
          },
          after: {
            deadline: {
              target: 'idle',
              actions: {
                type: 'fail',
                params: {
                  error: 'No admission reply. Retry uses the same request id.',
                  status: 'uncertain',
                },
              },
            },
          },
        },
      },
    },
  },
});

const INTENT_ACTIONS: Record<NonNullable<PostIntentRequest['intent']['case']>, IntentAction> = {
  pause: IntentAction.PAUSE,
  pauseNow: IntentAction.PAUSE_NOW,
  steer: IntentAction.STEER,
  withdraw: IntentAction.WITHDRAW,
  cancelAttempt: IntentAction.CANCEL_ATTEMPT,
  cancelItem: IntentAction.CANCEL_ITEM,
  setParallelism: IntentAction.SET_MAX_PARALLEL,
};

/** The newest epoch committed history names, which can lead the lifecycle stream. */
export function historyEpoch(context: RunIntentsContext): bigint {
  return context.outcomes.reduce(
    (newest, row) => (row.epoch !== null && row.epoch > newest ? row.epoch : newest),
    0n,
  );
}

/** History includes writer outcomes posted elsewhere, plus local requests still awaiting them. */
export function intentRows(context: RunIntentsContext, epoch: bigint): readonly IntentRow[] {
  const local = context.entries.map(entry => ({
    id: entry.request.requestId,
    source: 'intent' as const,
    action: entry.request.intent.case
      ? INTENT_ACTIONS[entry.request.intent.case]
      : IntentAction.UNSPECIFIED,
    status: entry.status,
    reason: entry.reason,
    epoch: entry.request.expectedEpoch,
  }));
  const rows = [
    ...context.outcomes.map(row => ({
      ...row,
      epoch: row.epoch ?? local.find(entry => entry.id === row.id)?.epoch ?? null,
    })),
    ...local.filter(
      row =>
        !context.outcomes.some(outcome => outcome.source === 'intent' && outcome.id === row.id),
    ),
  ];
  const latest = rows.filter(row => row.status === 'applied' && row.epoch === epoch).at(-1);
  return rows.map(row => ({
    ...row,
    current: latest !== undefined && row.id === latest.id && row.source === latest.source,
  }));
}
