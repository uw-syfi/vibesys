/**
 * The control state of one log stream on screen: an XState machine that reads its tail, then
 * follows it (`docs/contributing/desktop-connection.md`, "Logs").
 *
 * States: `idle` until `open`; `tailing` reads the last `tailBytes`; `following` keeps exactly one
 * `logs.read` in flight, from where the view ended, with `waitMs` so the helper long-polls at the
 * end of the stream; `closed` once a chunk reaches the end of a closed stream; `error` after a
 * failed read, which retries on its own after a growing delay when the failure is transient and
 * waits for `retry` when it is not (the stream or instance is unknown, the feature is missing);
 * `stopped` (final) on `stop`. Stopping the actor, as a view does when it unmounts, stops the read
 * in flight (its `signal` aborts) and every delay.
 *
 * The read is an operation the shell provides (`unprovided` here), so the machine stays pure; the
 * chunks fold through `foldChunk`, the package's data fold.
 */
import type {HelperErrorCode, LogChunk, LogOffset, LogTail} from '@vibesys/contracts/host-helper';
import {unprovided} from '@vibesys/machine';
import {assign, setup} from 'xstate';
import {
  DEFAULT_LOG_VIEW_BOUNDS,
  emptyLogView,
  foldChunk,
  type LogView,
  type LogViewBounds,
  logViewComplete,
} from './view.js';

/** One `logs.read` as the machine asks for it; the shell adds the request id and deadline. */
export interface LogReadInput {
  readonly instance_id: string;
  readonly stream_id: string;
  readonly start: LogOffset | LogTail;
  readonly max_bytes: number;
  readonly wait_ms: number;
}

/** Why a read failed: a helper error code, or `transport` when the channel itself failed. */
export type LogReadFailureCode = HelperErrorCode | 'transport';

export interface LogReadFailure {
  readonly code: LogReadFailureCode;
  readonly message: string;
}

/** What a read settles to; the shell reports expected failures as output, not as a rejection. */
export type LogReadOutcome =
  | {readonly ok: true; readonly chunk: LogChunk}
  | {readonly ok: false; readonly failure: LogReadFailure};

/**
 * Failures a retry cannot fix: the store has no such instance or stream or cannot be read, the
 * helper does not offer logs, or it refused the request as malformed. Anything else (a deadline, a busy or dropped
 * channel, a helper fault) is transient.
 */
const PERMANENT: ReadonlySet<LogReadFailureCode> = new Set<LogReadFailureCode>([
  'unknown_instance',
  'unknown_log_stream',
  'log_store_corrupt',
  'feature_not_negotiated',
  'invalid_request',
  'handshake_required',
  'incompatible',
]);

export function logReadRetryable(failure: LogReadFailure): boolean {
  return !PERMANENT.has(failure.code);
}

export interface LogFollowConfig {
  /** How much of the stream's end the first read fetches. */
  readonly tailBytes: number;
  /** The most bytes one read returns. */
  readonly maxBytes: number;
  /** How long a read at the end of the stream waits for more. */
  readonly waitMs: number;
  /** The delay before each automatic retry; the last repeats. */
  readonly retryDelaysMs: readonly number[];
  readonly bounds: LogViewBounds;
}

export const DEFAULT_LOG_FOLLOW_CONFIG: LogFollowConfig = {
  tailBytes: 64 * 1024,
  maxBytes: 64 * 1024,
  waitMs: 25_000,
  retryDelaysMs: [500, 1000, 2000, 5000, 10_000],
  bounds: DEFAULT_LOG_VIEW_BOUNDS,
};

export interface LogFollowInput {
  readonly instanceId: string;
  readonly streamId: string;
  readonly config?: LogFollowConfig;
}

export interface LogFollowContext {
  readonly instanceId: string;
  readonly streamId: string;
  readonly config: LogFollowConfig;
  readonly view: LogView;
  /** The last failure; cleared by the next chunk. */
  readonly failure: LogReadFailure | null;
  /** Consecutive failed reads; picks the retry delay. */
  readonly failures: number;
}

export type LogFollowEvent =
  | {readonly type: 'open'}
  | {readonly type: 'retry'}
  | {readonly type: 'stop'};

function readInput(context: LogFollowContext, start: LogOffset | LogTail): LogReadInput {
  return {
    instance_id: context.instanceId,
    stream_id: context.streamId,
    start,
    max_bytes: context.config.maxBytes,
    wait_ms: 'tail_bytes' in start ? 0 : context.config.waitMs,
  };
}

function outcomeFailure(output: LogReadOutcome): LogReadFailure | null {
  return output.ok ? null : output.failure;
}

function thrownFailure(error: unknown): LogReadFailure {
  return {code: 'transport', message: error instanceof Error ? error.message : String(error)};
}

export const logFollowMachine = setup({
  types: {
    context: {} as LogFollowContext,
    events: {} as LogFollowEvent,
    input: {} as LogFollowInput,
  },
  actors: {
    read: unprovided<LogReadOutcome, LogReadInput>('log read'),
  },
  guards: {
    failed: ({event}) => {
      const output = (event as {output?: LogReadOutcome}).output;
      return output !== undefined && !output.ok;
    },
    completes: ({context, event}) => {
      const output = (event as {output?: LogReadOutcome}).output;
      return output?.ok === true && logViewComplete(foldChunk(context.view, output.chunk));
    },
    started: ({context}) => context.view.nextOffset !== null,
    retryable: ({context}) => context.failure !== null && logReadRetryable(context.failure),
  },
  actions: {
    fold: assign(({context, event}) => {
      const output = (event as {output?: LogReadOutcome}).output;
      if (output === undefined || !output.ok) return {};
      return {view: foldChunk(context.view, output.chunk), failure: null, failures: 0};
    }),
    fail: assign(({context, event}) => {
      const settled = event as {output?: LogReadOutcome; error?: unknown};
      const failure =
        settled.output === undefined
          ? thrownFailure(settled.error)
          : outcomeFailure(settled.output);
      return {failure, failures: context.failures + 1};
    }),
  },
  delays: {
    retry: ({context}) => {
      const delays = context.config.retryDelaysMs;
      return delays[Math.min(context.failures, delays.length) - 1] ?? 0;
    },
  },
}).createMachine({
  id: 'logFollow',
  context: ({input}) => {
    const config = input.config ?? DEFAULT_LOG_FOLLOW_CONFIG;
    return {
      instanceId: input.instanceId,
      streamId: input.streamId,
      config,
      view: emptyLogView(config.bounds),
      failure: null,
      failures: 0,
    };
  },
  initial: 'idle',
  on: {stop: '.stopped'},
  states: {
    idle: {on: {open: 'tailing'}},
    tailing: {
      invoke: {
        id: 'readTail',
        src: 'read',
        input: ({context}) => readInput(context, {tail_bytes: context.config.tailBytes}),
        onDone: [
          {guard: 'failed', target: 'error', actions: 'fail'},
          {guard: 'completes', target: 'closed', actions: 'fold'},
          {target: 'following', actions: 'fold'},
        ],
        onError: {target: 'error', actions: 'fail'},
      },
    },
    following: {
      invoke: {
        id: 'readNext',
        src: 'read',
        input: ({context}) => readInput(context, {offset: context.view.nextOffset ?? 0}),
        onDone: [
          {guard: 'failed', target: 'error', actions: 'fail'},
          {guard: 'completes', target: 'closed', actions: 'fold'},
          {target: 'following', reenter: true, actions: 'fold'},
        ],
        onError: {target: 'error', actions: 'fail'},
      },
    },
    error: {
      initial: 'deciding',
      on: {retry: '.resuming'},
      states: {
        deciding: {
          always: [{guard: 'retryable', target: 'waiting'}, {target: 'held'}],
        },
        waiting: {after: {retry: 'resuming'}},
        held: {},
        resuming: {
          always: [
            {guard: 'started', target: '#logFollow.following'},
            {target: '#logFollow.tailing'},
          ],
        },
      },
    },
    closed: {},
    stopped: {type: 'final'},
  },
});
