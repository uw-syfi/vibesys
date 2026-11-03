/**
 * A `GatewayTransport` that plays back a `Recording` and records every call. It reads no bytes and
 * knows no service: a test, or a domain's own recordings, decide what the frames mean. Time is the
 * ambient clock, so a test under `TestClock` decides when each step happens.
 */
import * as Cause from 'effect/Cause';
import * as Deferred from 'effect/Deferred';
import type * as Duration from 'effect/Duration';
import * as Effect from 'effect/Effect';
import * as Layer from 'effect/Layer';
import * as Queue from 'effect/Queue';
import * as Stream from 'effect/Stream';
import {CallEnded, StreamOverflow} from '../../core/errors.js';
import {type CallTarget, GatewayTransport} from '../../core/transport/gateway-transport.js';

/** One thing a call does, in order. */
export type Step =
  /** Deliver a reply (the reply of a unary call, a frame of a stream). */
  | {readonly frame: Uint8Array}
  /** End the call with a failure. */
  | {readonly end: {readonly message?: string; readonly status?: number}}
  /** End a stream cleanly. */
  | {readonly close: true}
  /** Let virtual time pass. */
  | {readonly sleep: Duration.Input}
  /** Do nothing more until the call is cancelled. */
  | {readonly hang: true};

export interface Recording {
  /** What the `nth` call (0-based, counted per method) to `target` does. */
  readonly respond: (target: CallTarget, nth: number) => ReadonlyArray<Step>;
}

export interface CallRecord {
  readonly kind: 'unary' | 'stream';
  readonly target: CallTarget;
  /** Its index among the calls to the same method. */
  readonly nth: number;
  /** How many times the caller cancelled it while it was open: never more than one. */
  readonly cancelled: number;
  /** Neither ended by the recording nor cancelled yet. */
  readonly open: boolean;
}

export interface FakeTransport {
  readonly layer: Layer.Layer<GatewayTransport>;
  /** Every call so far, in the order it was opened. For assertions only. */
  readonly calls: () => ReadonlyArray<CallRecord>;
}

interface MutableCall {
  readonly kind: 'unary' | 'stream';
  readonly target: CallTarget;
  readonly nth: number;
  cancelled: number;
  open: boolean;
}

const endedBy = ({message, status}: {message?: string; status?: number}): CallEnded =>
  new CallEnded({message: message ?? '', ...(status === undefined ? {} : {status})});

/** Where a played step sends its effect; `false` from `frame` stops the playback. */
interface Sink {
  readonly frame: (bytes: Uint8Array) => Effect.Effect<boolean>;
  readonly failed: (end: CallEnded) => Effect.Effect<void>;
  readonly closed: Effect.Effect<void>;
}

/** Whether the playback goes on after a step. */
const runStep = (step: Step, sink: Sink): Effect.Effect<boolean> => {
  if ('sleep' in step) return Effect.as(Effect.sleep(step.sleep), true);
  if ('frame' in step) return sink.frame(step.frame);
  if ('end' in step) return Effect.as(sink.failed(endedBy(step.end)), false);
  if ('close' in step) return Effect.as(sink.closed, false);
  return Effect.never;
};

const play = (steps: ReadonlyArray<Step>, sink: Sink): Effect.Effect<void> =>
  Effect.gen(function* () {
    for (const step of steps) {
      if (!(yield* runStep(step, sink))) return;
    }
    yield* sink.closed;
  });

export const makeFakeTransport = (recording: Recording): FakeTransport => {
  const calls: MutableCall[] = [];
  const countByMethod = new Map<string, number>();

  const open = (kind: 'unary' | 'stream', target: CallTarget): MutableCall => {
    const nth = countByMethod.get(target.method) ?? 0;
    countByMethod.set(target.method, nth + 1);
    const call: MutableCall = {kind, target, nth, cancelled: 0, open: true};
    calls.push(call);
    return call;
  };
  /** The caller gave up on an open call. Idempotent, as the gateway's cancel is. */
  const cancel = (call: MutableCall): void => {
    if (!call.open) return;
    call.open = false;
    call.cancelled += 1;
  };

  const unary: GatewayTransport['Service']['unary'] = target =>
    Effect.gen(function* () {
      const call = open('unary', target);
      const answer = yield* Deferred.make<Uint8Array, CallEnded>();
      const sink: Sink = {
        frame: bytes => Effect.as(Deferred.succeed(answer, bytes), false),
        failed: end => Effect.asVoid(Deferred.fail(answer, end)),
        closed: Effect.asVoid(
          Deferred.fail(answer, new CallEnded({message: 'missing unary response'})),
        ),
      };
      yield* Effect.forkChild(play(recording.respond(target, call.nth), sink));
      return yield* Deferred.await(answer).pipe(
        Effect.onInterrupt(() => Effect.sync(() => cancel(call))),
        Effect.ensuring(
          Effect.sync(() => {
            call.open = false;
          }),
        ),
      );
    });

  const stream: GatewayTransport['Service']['stream'] = (target, capacity) =>
    Stream.callback<Uint8Array, CallEnded | StreamOverflow>(
      Effect.fnUntraced(function* (queue) {
        const call = open('stream', target);
        yield* Effect.addFinalizer(() => Effect.sync(() => cancel(call)));
        const finish = (end: () => void): Effect.Effect<void> =>
          Effect.sync(() => {
            call.open = false;
            end();
          });
        const sink: Sink = {
          frame: bytes =>
            Effect.sync(() => {
              if (Queue.offerUnsafe(queue, bytes)) return true;
              cancel(call);
              Queue.failCauseUnsafe(queue, Cause.fail(new StreamOverflow({capacity})));
              return false;
            }),
          failed: end => finish(() => Queue.failCauseUnsafe(queue, Cause.fail(end))),
          closed: finish(() => Queue.endUnsafe(queue)),
        };
        yield* Effect.forkScoped(play(recording.respond(target, call.nth), sink));
      }),
      {bufferSize: capacity},
    );

  return {
    layer: Layer.succeed(GatewayTransport, {unary, stream}),
    calls: () => calls.map(call => ({...call})),
  };
};
