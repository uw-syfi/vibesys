/**
 * gRPC over one HTTP/2 session on a duplex the caller dialed. The session owns the duplex: it ends
 * with the scope it was opened in. A call is one HTTP/2 stream; interrupting the call resets it.
 */
import {type ClientHttp2Session, connect, constants, type IncomingHttpHeaders} from 'node:http2';
import type {Duplex} from 'node:stream';
import {ORCHESTRATOR_MAX_MESSAGE_BYTES, OrchestratorService} from '@vibesys/orchestrator-proto';
import {HOST_API_MAX_MESSAGE_BYTES} from '@vibesys/proto';
import * as Deferred from 'effect/Deferred';
import * as Effect from 'effect/Effect';
import * as Exit from 'effect/Exit';
import type * as Scope from 'effect/Scope';
import {REPLY_STALL_MS, SLOW_ANSWER_BOUND_MS} from '../state/gateway-policy.js';
import {decodeFrames, encodeFrame, isFault} from '../state/grpc-frames.js';
import {callStatus} from '../state/grpc-status.js';
import {GatewayFailed, GrpcCode, GrpcFailed} from './errors.js';

/** How long a call is watched for progress: to its end, or until its first reply message. */
type Watch = 'to-end' | 'to-first-reply';

/**
 * How a call fails: `GrpcFailed` is the host's own answer (a status it sent); `GatewayFailed` is
 * the link's: the session or stream was cut, or answered with no status. The two are kept apart
 * because only a host that answered can be asked why, and a lost link is redialed.
 */
export type CallFailed = GrpcFailed | GatewayFailed;

export interface GatewaySession {
  /**
   * One call: send `bytes`, hand each reply message to `receive` in order, end with the status.
   * `receive` throwing ends the call with INTERNAL. Interrupting the call resets its stream. With
   * `watch`, the call fails with DEADLINE_EXCEEDED in two cases, told apart by what the link
   * delivers. When a whole `REPLY_STALL_MS` window passes with no byte heard on the link (a frame,
   * a chunk of one, a connection-level frame such as a keepalive or the answer to a probe) the link
   * is silent and the call is cut. While the link keeps delivering, the host is alive and merely
   * slow: the call waits, and fails only once `SLOW_ANSWER_BOUND_MS` passes with nothing of its own
   * answered. Without `watch` it is bounded by the session's liveness alone, as a quiet feed must be.
   */
  readonly call: (
    method: string,
    bytes: Uint8Array,
    receive: (message: Uint8Array) => void,
    watch?: Watch,
  ) => Effect.Effect<void, CallFailed>;
  /** A call with exactly one reply, watched to its end. */
  readonly unary: (method: string, bytes: Uint8Array) => Effect.Effect<Uint8Array, CallFailed>;
  /** Completes when the session ended: the peer closed it, said goaway, or the scope closed. */
  readonly lost: Effect.Effect<void>;
  /**
   * How many chunks the link has delivered since the session opened, counted on the duplex itself
   * so a connection-level frame (SETTINGS, PING) and the middle of a frame count as much as a call
   * event. It only rises, so two reads that differ prove the link delivered bytes between them; a
   * link that trickles bytes rises with every chunk, which is how a slow link is told from a dead
   * one.
   */
  readonly activity: Effect.Effect<number>;
  /**
   * A unary call that asks the link whether it is alive, for the liveness monitor. Its reply is
   * bytes, so it counts as `activity` and shows the link is alive to every watched call; it is not
   * an answer to any of them, so a call the host never answers is still ended at
   * `SLOW_ANSWER_BOUND_MS`. It is not watched itself: whether the link answers is the monitor's
   * verdict.
   */
  readonly probe: (method: string, bytes: Uint8Array) => Effect.Effect<Uint8Array, CallFailed>;
}

const METHOD = /^\/[A-Za-z][\w.]*\/[A-Za-z]\w*$/;

type Stream = ReturnType<ClientHttp2Session['request']>;
type Receive = (message: Uint8Array) => void;

const cut = (message: string): GatewayFailed => new GatewayFailed({message});

/** The metadata every call of a session carries: gRPC metadata is HTTP/2 headers, in lowercase. */
export type CallMetadata = Readonly<Record<string, string>>;

/** Start the HTTP/2 stream of one call, or say why it cannot start. */
const startStream = (
  http2: ClientHttp2Session,
  method: string,
  frame: Uint8Array,
  metadata: CallMetadata,
): Stream | CallFailed => {
  if (!METHOD.test(method))
    return new GrpcFailed({code: GrpcCode.INTERNAL, message: `invalid gRPC method: ${method}`});
  try {
    const stream = http2.request({
      ...metadata,
      ':method': 'POST',
      ':path': method,
      'content-type': 'application/grpc',
      te: 'trailers',
    });
    stream.end(frame);
    return stream;
  } catch (cause) {
    return cut(`gateway session closed: ${String(cause)}`);
  }
};

/**
 * Read one call's stream: reply messages to `receive` in order, then the status the peer ended it
 * with. `finish` is told the failure, or nothing for a call that ended with status OK.
 */
const watchStream = (
  stream: Stream,
  receive: Receive,
  finish: (error?: CallFailed) => void,
  settled: () => boolean,
  receiveLimit: number,
): void => {
  let rest: Uint8Array = new Uint8Array(0);
  let status: string | undefined;
  let detail = '';
  const headers = (value: IncomingHttpHeaders): void => {
    if (value['grpc-status'] !== undefined) status = String(value['grpc-status']);
    if (value['grpc-message'] !== undefined) detail = String(value['grpc-message']);
    const http = value[':status'];
    if (http !== undefined && String(http) !== '200')
      finish(cut(`gRPC HTTP status ${String(http)}`));
  };
  const deliver = (messages: readonly Uint8Array[]): void => {
    for (const message of messages) {
      try {
        receive(message);
      } catch (cause) {
        // A consumer that refuses with a status of its own (a reply that breaks the schema) ends
        // the call with it.
        finish(
          cause instanceof GrpcFailed
            ? cause
            : new GrpcFailed({
                code: GrpcCode.INTERNAL,
                message: `gateway consumer failed: ${String(cause)}`,
              }),
        );
        return;
      }
    }
  };
  stream.on('response', headers);
  stream.on('trailers', headers);
  stream.on('error', error => finish(cut(error.message)));
  stream.on('data', (chunk: Buffer) => {
    if (settled()) return;
    const decoded = decodeFrames(rest, chunk, receiveLimit);
    if (isFault(decoded)) return finish(new GrpcFailed(decoded));
    rest = decoded.rest;
    deliver(decoded.messages);
  });
  stream.on('end', () => {
    // A stream that ends with no status was cut, not answered: it is the link's failure, which
    // the client redials, rather than a status of the host's own.
    if (rest.byteLength !== 0)
      return finish(new GrpcFailed({code: GrpcCode.DATA_LOSS, message: 'truncated gRPC message'}));
    const failure = callStatus(status, detail);
    if (failure === undefined) return finish(undefined);
    finish(
      failure.answered ? new GrpcFailed(failure) : new GatewayFailed({message: failure.message}),
    );
  });
  stream.on('close', () => finish(cut('gRPC connection closed')));
};

/** What one call has delivered, which its watch reads. */
interface Tally {
  replied: boolean;
  /** How many events of this call's own stream arrived (headers, a chunk of data, trailers). */
  own: number;
}

/** What a call needs of its session: the connection, what the link was heard to deliver, and the metadata of every call. */
interface Line {
  readonly http2: ClientHttp2Session;
  /** How many chunks the link has delivered: any byte shows the host is alive. */
  readonly heard: () => number;
  readonly metadata: CallMetadata;
}

/** The send of one call over `line`; its progress is counted in `tally`. */
const startCall = (
  {http2, metadata}: Line,
  tally: Tally,
  method: string,
  bytes: Uint8Array,
  receive: Receive,
): Effect.Effect<void, CallFailed> =>
  Effect.callback<void, CallFailed>(resume => {
    const frame = encodeFrame(bytes, HOST_API_MAX_MESSAGE_BYTES);
    if (isFault(frame)) return resume(Effect.fail(new GrpcFailed(frame)));
    const stream = startStream(http2, method, frame, metadata);
    if (stream instanceof GrpcFailed || stream instanceof GatewayFailed)
      return resume(Effect.fail(stream));
    let settled = false;
    for (const event of ['response', 'trailers', 'data'] as const)
      stream.on(event, () => {
        tally.own += 1;
      });
    watchStream(
      stream,
      message => {
        tally.replied = true;
        receive(message);
      },
      error => {
        if (settled) return;
        settled = true;
        if (error === undefined) resume(Effect.void);
        else {
          stream.close(constants.NGHTTP2_CANCEL);
          resume(Effect.fail(error));
        }
      },
      () => settled,
      method.startsWith(`/${OrchestratorService.typeName}/`)
        ? ORCHESTRATOR_MAX_MESSAGE_BYTES
        : HOST_API_MAX_MESSAGE_BYTES,
    );
    return Effect.sync(() => {
      if (settled) return;
      settled = true;
      stream.close(constants.NGHTTP2_CANCEL);
    });
  });

/**
 * Fails a call the host is not answering, by what the link shows. Bytes are counted on the
 * duplex, not at the call's own events: an HTTP/2 frame only becomes an event once it is whole, so
 * a link slower than a frame per window would read as silent while it delivers.
 *
 * - A window with no byte on the link: the link is silent, nothing says the host is there, and the
 *   call is cut at once.
 * - A window with bytes but nothing of the call's own: the link is alive and the host slow (a
 *   keepalive, the answer to a probe or another call's reply all arrive). The call waits, up to
 *   `SLOW_ANSWER_BOUND_MS` without a reply of its own, which ends a host that never answers it.
 *
 * Never ends otherwise: it is raced against the call, and a watch `to-first-reply` stops
 * watching once the first reply came.
 */
const stalled = (line: Line, tally: Tally, watch: Watch): Effect.Effect<never, CallFailed> =>
  Effect.gen(function* () {
    let unanswered = 0;
    while (true) {
      const heard = line.heard();
      const own = tally.own;
      yield* Effect.sleep(`${REPLY_STALL_MS} millis`);
      if (watch === 'to-first-reply' && tally.replied) return yield* Effect.never;
      if (line.heard() === heard)
        return yield* new GrpcFailed({
          code: GrpcCode.DEADLINE_EXCEEDED,
          message: `gRPC deadline exceeded: nothing heard for ${REPLY_STALL_MS / 1000} seconds`,
        });
      unanswered = tally.own === own ? unanswered + REPLY_STALL_MS : 0;
      if (unanswered >= SLOW_ANSWER_BOUND_MS)
        return yield* new GrpcFailed({
          code: GrpcCode.DEADLINE_EXCEEDED,
          message: `gRPC deadline exceeded: no answer for ${SLOW_ANSWER_BOUND_MS / 1000} seconds, though the link is alive`,
        });
    }
  });

/** One call over `line`: send `bytes`, then `watchStream`. Interrupting resets the stream. */
const callOver =
  (line: Line): GatewaySession['call'] =>
  (method, bytes, receive, watch) =>
    Effect.suspend(() => {
      const tally: Tally = {replied: false, own: 0};
      const run = startCall(line, tally, method, bytes, receive);
      return watch === undefined ? run : Effect.raceFirst(run, stalled(line, tally, watch));
    });

/** A call with exactly one reply, watched to its end unless `watch` is `none`. */
const unaryOver =
  (call: GatewaySession['call'], watch: Watch | 'none' = 'to-end'): GatewaySession['unary'] =>
  (method, bytes) =>
    Effect.suspend(() => {
      let reply: Uint8Array | undefined;
      return call(
        method,
        bytes,
        message => {
          if (reply !== undefined) throw new Error('unary returned multiple messages');
          reply = message;
        },
        watch === 'none' ? undefined : watch,
      ).pipe(
        Effect.flatMap(() =>
          reply === undefined
            ? Effect.fail(
                new GrpcFailed({code: GrpcCode.INTERNAL, message: 'unary returned no message'}),
              )
            : Effect.succeed(reply),
        ),
      );
    });

/**
 * Open the session on `duplex`; it is destroyed with `scope`. Every call of the session carries
 * `metadata`, which the host reads (who is calling: `state/viewer.ts`).
 */
export const openSession = (
  duplex: Duplex,
  metadata: CallMetadata = {},
): Effect.Effect<GatewaySession, never, Scope.Scope> =>
  Effect.gen(function* () {
    const lost = Deferred.makeUnsafe<void>();
    let heard = 0;
    const markLost = (): void => {
      Deferred.doneUnsafe(lost, Exit.void);
    };
    const http2: ClientHttp2Session = yield* Effect.acquireRelease(
      Effect.sync(() => {
        const session = connect('http://vibesys', {createConnection: () => duplex});
        // Calls receive their own errors; an idle session still needs an error owner.
        session.on('error', () => {});
        session.once('close', markLost);
        session.once('goaway', markLost);
        // Counted here, on the link, before the session parses anything: see `activity`.
        duplex.on('data', () => {
          heard += 1;
        });
        return session;
      }),
      session =>
        Effect.sync(() => {
          session.destroy();
          markLost();
        }),
    );
    const call = callOver({http2, heard: () => heard, metadata});
    const probeCall = unaryOver(call, 'none');
    return {
      call,
      unary: unaryOver(call),
      probe: probeCall,
      lost: Deferred.await(lost),
      activity: Effect.sync(() => heard),
    } satisfies GatewaySession;
  });
