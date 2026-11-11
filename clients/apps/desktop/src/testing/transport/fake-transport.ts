/**
 * A `GatewayTransport` that plays back a `Recording` and records every call. It reads no bytes and
 * knows no service: a test, or a domain's own recordings, decide what the frames mean. Time is the
 * ambient clock, so a test under `TestClock` decides when each step happens.
 */
import type {DesktopLinkProblem, DesktopRefusal} from '@vibesys/proto';
import * as Cause from 'effect/Cause';
import * as Deferred from 'effect/Deferred';
import type * as Duration from 'effect/Duration';
import * as Effect from 'effect/Effect';
import * as Layer from 'effect/Layer';
import * as Queue from 'effect/Queue';
import * as Stream from 'effect/Stream';
import {CallEnded, StreamOverflow} from '../../core/errors.js';
import {type CallTarget, GatewayTransport} from '../../core/transport/gateway-transport.js';
import {invokeCallback} from '../callback-errors.js';

/** One thing a call does, in order. */
export type Step =
  /** Deliver a reply (the reply of a unary call, a frame of a stream). */
  | {readonly frame: Uint8Array}
  /** End the call with a failure. */
  | {
      readonly end: {
        readonly message?: string;
        readonly status?: number;
        readonly linkProblem?: DesktopLinkProblem;
        readonly refusal?: DesktopRefusal;
      };
    }
  /** End a stream cleanly. */
  | {readonly close: true}
  /** Fail a stream as a reader too slow for it does: its buffer is full. */
  | {readonly overflow: true}
  /** Let virtual time pass. */
  | {readonly sleep: Duration.Input}
  /** Do nothing more until the call is given up on. */
  | {readonly hang: true}
  /**
   * Deliver each frame `follow` emits, as it is emitted, until the call is given up on: a stream
   * that stays open and carries what happens after it opened. `follow` returns how to stop.
   */
  | {readonly follow: (emit: (frame: Uint8Array) => void) => () => void};

export interface Recording {
  /** What the `nth` call (0-based, counted per method) to `target` does. */
  readonly respond: (target: CallTarget, nth: number) => ReadonlyArray<Step>;
}

export interface CallRecord {
  readonly kind: 'unary' | 'stream';
  readonly target: CallTarget;
  /** Its index among the calls to the same method. */
  readonly nth: number;
  /**
   * How many times the call's resources were released: once the call is over, however it ended
   * (the recording ended it, or the caller gave up), exactly once; while it is open, never. Counted
   * at the single place that releases, so a second release shows as 2.
   */
  readonly released: number;
  /** Neither ended nor given up on yet. */
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
  released: number;
  open: boolean;
}

const endedBy = ({
  message,
  status,
  linkProblem,
  refusal,
}: {
  message?: string;
  status?: number;
  linkProblem?: DesktopLinkProblem;
  refusal?: DesktopRefusal;
}): CallEnded =>
  new CallEnded({
    message: message ?? '',
    ...(status === undefined ? {} : {status}),
    ...(linkProblem === undefined ? {} : {linkProblem}),
    ...(refusal === undefined ? {} : {refusal}),
  });

/** Where a played step sends its effect; `false` from `frame` stops the playback. */
interface Sink {
  readonly frame: (bytes: Uint8Array) => Effect.Effect<boolean>;
  readonly failed: (end: CallEnded) => Effect.Effect<void>;
  readonly closed: Effect.Effect<void>;
  readonly overflowed: Effect.Effect<void>;
}

/** Delivers what `follow` emits until the sink refuses a frame or the call is given up on. */
const followed = (follow: (emit: (frame: Uint8Array) => void) => () => void, sink: Sink) =>
  Effect.gen(function* () {
    const frames = yield* Queue.unbounded<Uint8Array>();
    return yield* Effect.acquireUseRelease(
      Effect.sync(() =>
        follow(frame => {
          Queue.offerUnsafe(frames, frame);
        }),
      ),
      () =>
        Effect.gen(function* () {
          while (true) {
            if (!(yield* sink.frame(yield* Queue.take(frames)))) return false;
          }
        }),
      stop => Effect.sync(stop),
    );
  });

/** Whether the playback goes on after a step. */
const runStep = (step: Step, sink: Sink): Effect.Effect<boolean> => {
  if ('sleep' in step) return Effect.as(Effect.sleep(step.sleep), true);
  if ('frame' in step) return sink.frame(step.frame);
  if ('end' in step) return Effect.as(sink.failed(endedBy(step.end)), false);
  if ('close' in step) return Effect.as(sink.closed, false);
  if ('overflow' in step) return Effect.as(sink.overflowed, false);
  if ('follow' in step) return followed(step.follow, sink);
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
    const call: MutableCall = {kind, target, nth, released: 0, open: true};
    calls.push(call);
    return call;
  };
  /** Release a call's resources: called once per call, by the construct every exit passes through. */
  const release = (call: MutableCall): void => {
    call.open = false;
    call.released += 1;
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
        overflowed: Effect.asVoid(
          Deferred.fail(answer, new CallEnded({message: 'a unary reply has no buffer to fill'})),
        ),
      };
      yield* Effect.forkChild(
        play(
          invokeCallback(() => recording.respond(target, call.nth)),
          sink,
        ),
      );
      return yield* Deferred.await(answer).pipe(Effect.ensuring(Effect.sync(() => release(call))));
    });

  const stream: GatewayTransport['Service']['stream'] = (target, capacity) =>
    Stream.callback<Uint8Array, CallEnded | StreamOverflow>(
      Effect.fnUntraced(function* (queue) {
        const call = open('stream', target);
        yield* Effect.addFinalizer(() => Effect.sync(() => release(call)));
        const finish = (end: () => void): Effect.Effect<void> =>
          Effect.sync(() => {
            call.open = false;
            end();
          });
        const sink: Sink = {
          frame: bytes =>
            Effect.sync(() => {
              if (Queue.offerUnsafe(queue, bytes)) return true;
              call.open = false;
              Queue.failCauseUnsafe(queue, Cause.fail(new StreamOverflow({capacity})));
              return false;
            }),
          failed: end => finish(() => Queue.failCauseUnsafe(queue, Cause.fail(end))),
          closed: finish(() => Queue.endUnsafe(queue)),
          overflowed: finish(() =>
            Queue.failCauseUnsafe(queue, Cause.fail(new StreamOverflow({capacity}))),
          ),
        };
        yield* Effect.forkScoped(
          play(
            invokeCallback(() => recording.respond(target, call.nth)),
            sink,
          ),
        );
      }),
      {bufferSize: capacity},
    );

  return {
    layer: Layer.succeed(GatewayTransport, {unary, stream}),
    calls: () => calls.map(call => ({...call})),
  };
};
