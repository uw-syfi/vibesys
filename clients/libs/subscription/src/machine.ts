/**
 * The lifetime of one subscription as a state machine: open a stream, wait for its snapshot, keep a
 * lease on its frames, and on any loss back off and open another. It owns every wait, so a screen
 * cannot wait forever: with no frames at all, the machine reaches a `failed` answer in a bounded
 * time (`failureBoundMs`), whatever the layers below do.
 *
 * Main runs one machine per host and topic instance; windows only fold what it emits. A stream is
 * an operation the shell provides: it opens the gateway call, reports `StreamEvent`s through
 * `receive`, and resolves or rejects when the stream ends. Leaving `streaming` stops the operation,
 * which aborts the call, so a stream never outlives the state that owns it.
 */
import {unprovided} from '@vibesys/machine';
import {subscribeTiming} from '@vibesys/proto';
import {assign, emit, raise, type SnapshotFrom, setup} from 'xstate';
import {type DataUpdate, EMPTY_FOLD, type Fold, foldUpdate} from './fold.js';
import type {StreamEvent} from './frames.js';
import {
  CONNECT_STEPS,
  type ConnectStep,
  type Control,
  type Instant,
  OFFLINE_AFTER,
  retryable,
  type StaleReason,
} from './state.js';
import type {AnyTopic} from './topics.js';

/** The delay before the first reconnect; it doubles up to `BACKOFF_MAX_MS`. */
const BACKOFF_FIRST_MS = 1_000;
const BACKOFF_MAX_MS = 30_000;
/** After `OFFLINE_AFTER` failures a host is tried this often. */
const PARKED_RETRY_MS = 30_000;
/** After the machine wakes, a live stream must show a frame within one beat and this much more. */
const WAKE_GRACE_MS = 2_000;

/** The delay after the `attempt`th failure in a row. */
export function backoffMs(attempt: number): number {
  return Math.min(BACKOFF_FIRST_MS * 2 ** Math.max(attempt - 1, 0), BACKOFF_MAX_MS);
}

/** What the shell provides: report the stream's events here, and settle when the stream ends. */
export interface StreamOperation {
  readonly receive: (event: StreamEvent) => void;
}

export interface SubscriptionInput {
  readonly topic: AnyTopic;
  /** The longest a stream may take to open, from the connection owner that dials it. */
  readonly openBudgetMs: number;
}

interface Loss {
  readonly reason: StaleReason;
  /** Failures since the stream was last live. */
  readonly attempt: number;
}

interface Context extends SubscriptionInput {
  /** The latest loss, or null before any. */
  loss: Loss | null;
  step: ConnectStep;
  since: Instant;
  /** When the latest value arrived: a snapshot, or an entry after it. */
  valueAt: Instant;
  fold: Fold;
  halted: 'unsupported' | 'invalid_frame' | null;
}

type Event = StreamEvent | {type: 'retry'} | {type: 'wake'} | {type: 'close'};

/** The data updates the machine accepted, for the windows that fold them. */
type Emitted = {type: 'update'; update: DataUpdate};

type ChangeEvent = Extract<Event, {type: 'entry' | 'removed' | 'synced'}>;

/** The fold's update for a change event; `synced` and the rest have no payload to project. */
function updateOf(event: ChangeEvent): DataUpdate {
  switch (event.type) {
    case 'entry':
      return {kind: 'entry', entry: event.entry};
    case 'removed':
      return {kind: 'removed', key: event.key};
    case 'synced':
      return {kind: 'synced'};
  }
}

function lose(context: Context, reason: StaleReason): Loss {
  return {reason, attempt: (context.loss?.attempt ?? 0) + 1};
}

/** Fold a change event and pass the update on. */
/**
 * A frame of the stream's data before the stream said it reached the host: the stream is open, so
 * it is awaiting its snapshot, and the frame is read there. It is never dropped, which would let
 * a snapshot complete on the entries that followed it.
 */
const implied = {target: 'awaiting', actions: ['reached', 'again']} as const;

const changed = [
  {type: 'apply', params: ({event}: {event: ChangeEvent}) => updateOf(event)},
  {type: 'forward', params: ({event}: {event: ChangeEvent}) => updateOf(event)},
] as const;

export const subscriptionMachine = setup({
  types: {
    context: {} as Context,
    input: {} as SubscriptionInput,
    events: {} as Event,
    emitted: {} as Emitted,
  },
  actors: {stream: unprovided<void, StreamOperation>('subscription stream')},
  actions: {
    // The fold's reducer is the data's only writer; the update is also what windows fold.
    apply: assign({
      fold: ({context}, update: DataUpdate) => foldUpdate(context.topic, context.fold, update),
    }),
    forward: emit((_, update: DataUpdate): Emitted => ({type: 'update', update})),
    valued: assign({
      valueAt: ({event}) => (event.type === 'entry' ? event.at : 0),
    }),
    reached: assign({step: 'stream' as const}),
    // The event is read again in the state it moved to.
    again: raise(({event}) => event),
    restart: assign({
      loss: ({context}) => (context.loss === null ? null : {...context.loss, attempt: 0}),
    }),
  },
  guards: {
    broken: ({context}) => context.fold.broken,
    atStream: ({event}) => event.type === 'reached' && event.step === 'stream',
    gaveUp: ({context}) => (context.loss?.attempt ?? 0) >= OFFLINE_AFTER,
    // A heartbeat proves the stream, not the value: a sampled topic must keep sending values.
    valueBounded: ({context}) => context.topic.policy.staleAfterMs > 0,
    final: ({event}) => event.type === 'refused' && !retryable(event.reason),
  },
  delays: {
    openBudget: ({context}) => context.openBudgetMs,
    firstFrame: ({context}) => context.topic.policy.firstFrameMs,
    valueBound: ({context}) => context.topic.policy.staleAfterMs,
    lease: subscribeTiming.leaseMs,
    wakeLease: subscribeTiming.heartbeatMs + WAKE_GRACE_MS,
    backoff: ({context}) => backoffMs(context.loss?.attempt ?? 0),
    parked: PARKED_RETRY_MS,
  },
}).createMachine({
  id: 'subscription',
  context: ({input}) => ({
    ...input,
    loss: null,
    step: CONNECT_STEPS[0],
    since: 0,
    valueAt: 0,
    fold: EMPTY_FOLD,
    halted: null,
  }),
  initial: 'streaming',
  on: {close: {target: '#subscription.closed'}},
  states: {
    streaming: {
      entry: [
        assign({
          step: CONNECT_STEPS[0],
          fold: ({context}) => foldUpdate(context.topic, context.fold, {kind: 'begin'}),
        }),
        emit((): Emitted => ({type: 'update', update: {kind: 'begin'}})),
      ],
      invoke: {
        id: 'stream',
        src: 'stream',
        input: ({self}) => ({receive: event => self.send(event)}),
        // A stream that ends has lost its connection, wherever it had come.
        onDone: {
          target: 'lost',
          actions: assign({
            loss: ({context}) => lose(context, context.step === 'stream' ? 'ended' : 'unreachable'),
          }),
        },
        onError: {
          target: 'lost',
          actions: assign({
            loss: ({context}) => lose(context, context.step === 'stream' ? 'ended' : 'unreachable'),
          }),
        },
      },
      // A frame that broke the protocol ends the subscription: no retry would fare better.
      always: {
        guard: 'broken',
        target: 'halted',
        actions: assign({halted: 'invalid_frame' as const}),
      },
      on: {
        refused: [
          {
            guard: 'final',
            target: 'halted',
            actions: assign({
              halted: ({event}) =>
                event.reason === 'unsupported' ? 'unsupported' : 'invalid_frame',
            }),
          },
          {
            target: 'lost',
            actions: assign({
              loss: ({context, event}) =>
                lose(context, retryable(event.reason) ? event.reason : 'internal'),
            }),
          },
        ],
      },
      initial: 'dialing',
      states: {
        dialing: {
          after: {
            openBudget: {
              target: '#subscription.lost',
              actions: assign({loss: ({context}) => lose(context, 'unreachable')}),
            },
          },
          on: {
            reached: [
              {guard: 'atStream', target: 'awaiting', actions: assign({step: 'stream' as const})},
              {actions: assign({step: ({event}) => event.step})},
            ],
            entry: implied,
            removed: implied,
            synced: implied,
          },
        },
        awaiting: {
          after: {
            firstFrame: {
              target: '#subscription.lost',
              actions: assign({loss: ({context}) => lose(context, 'no_first_frame')}),
            },
          },
          on: {
            entry: {actions: changed},
            removed: {actions: changed},
            synced: {
              target: 'live',
              actions: [
                ...changed,
                assign({since: ({event}) => event.at, valueAt: ({event}) => event.at, loss: null}),
              ],
            },
          },
        },
        // The link and the value are bounded apart: a stream can be alive and its value old.
        live: {
          type: 'parallel',
          states: {
            link: {
              initial: 'leased',
              states: {
                leased: {
                  after: {
                    lease: {
                      target: '#subscription.lost',
                      actions: assign({loss: ({context}) => lose(context, 'silent')}),
                    },
                  },
                  on: {
                    beat: {target: 'leased', reenter: true},
                    entry: {target: 'leased', reenter: true, actions: [...changed, 'valued']},
                    removed: {target: 'leased', reenter: true, actions: changed},
                    synced: {target: 'leased', reenter: true, actions: changed},
                    wake: 'suspect',
                  },
                },
                // The machine woke: the stream may be dead, and a frame within a beat says it is not.
                suspect: {
                  after: {
                    wakeLease: {
                      target: '#subscription.lost',
                      actions: assign({loss: ({context}) => lose(context, 'silent')}),
                    },
                  },
                  on: {
                    beat: 'leased',
                    entry: {target: 'leased', actions: [...changed, 'valued']},
                    removed: {target: 'leased', actions: changed},
                    synced: {target: 'leased', actions: changed},
                  },
                },
              },
            },
            // A value older than the topic allows ends the stream whatever its beats say. The
            // bound is a timer like every other wait, so a clock that steps cannot stretch it.
            value: {
              initial: 'judging',
              states: {
                judging: {
                  always: [{guard: 'valueBounded', target: 'fresh'}, {target: 'unbounded'}],
                },
                fresh: {
                  after: {
                    valueBound: {
                      target: '#subscription.lost',
                      actions: assign({loss: ({context}) => lose(context, 'outdated')}),
                    },
                  },
                  on: {entry: {target: 'fresh', reenter: true}},
                },
                unbounded: {},
              },
            },
          },
        },
      },
    },
    // Where every loss passes: count it, then back off, or park once the host keeps failing.
    lost: {
      always: [{guard: 'gaveUp', target: 'parked'}, {target: 'backoff'}],
    },
    backoff: {
      after: {backoff: 'streaming'},
      on: {
        retry: {target: 'streaming', actions: 'restart'},
        wake: {target: 'streaming', actions: 'restart'},
      },
    },
    parked: {
      after: {parked: 'streaming'},
      on: {
        retry: {target: 'streaming', actions: 'restart'},
        wake: {target: 'streaming', actions: 'restart'},
      },
    },
    halted: {
      on: {retry: {target: 'streaming', actions: ['restart', assign({halted: null})]}},
    },
    closed: {type: 'final'},
  },
});

export type SubscriptionSnapshot = SnapshotFrom<typeof subscriptionMachine>;

/**
 * What the machine says now, from its state and context alone. Failing is derived, not stored: a
 * host that has lost `OFFLINE_AFTER` streams in a row is `failed` until one is live again, so a
 * screen sees one answer instead of a flicker between failed and trying.
 */
export function controlOf(snapshot: SubscriptionSnapshot): Control {
  const {context} = snapshot;
  if (snapshot.matches('closed')) return {kind: 'failed', reason: 'closed', retrying: false};
  if (snapshot.matches('halted') && context.halted !== null)
    return {kind: 'failed', reason: context.halted, retrying: false};
  if (snapshot.matches({streaming: 'live'})) return {kind: 'live', since: context.since};
  const {loss} = context;
  if (loss === null) return {kind: 'connecting', step: context.step, attempt: 0};
  if (loss.attempt >= OFFLINE_AFTER) return {kind: 'failed', reason: loss.reason, retrying: true};
  if (context.fold.map !== null)
    return {kind: 'stale', reason: loss.reason, attempt: loss.attempt, valueAt: context.valueAt};
  return {kind: 'connecting', step: context.step, attempt: loss.attempt};
}

/**
 * The longest a subscription that receives nothing takes to answer `failed`: `OFFLINE_AFTER`
 * attempts, each at most the open budget plus the wait for a first frame, and the backoffs between.
 */
export function failureBoundMs(topic: AnyTopic, openBudgetMs: number): number {
  let total = OFFLINE_AFTER * (openBudgetMs + topic.policy.firstFrameMs);
  for (let attempt = 1; attempt < OFFLINE_AFTER; attempt += 1) total += backoffMs(attempt);
  return total;
}
