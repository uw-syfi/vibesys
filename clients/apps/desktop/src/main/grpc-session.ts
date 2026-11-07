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
import {REPLY_STALL_MS} from '../state/gateway-policy.js';
import {decodeFrames, encodeFrame, isFault} from '../state/grpc-frames.js';
import {callStatus} from '../state/grpc-status.js';
import {GrpcCode, GrpcFailed} from './errors.js';

/** How long a call is watched for progress: to its end, or until its first reply message. */
type Watch = 'to-end' | 'to-first-reply';

export interface GatewaySession {
  /**
   * One call: send `bytes`, hand each reply message to `receive` in order, end with the status.
   * `receive` throwing ends the call with INTERNAL. Interrupting the call resets its stream. With
   * `watch`, the call fails with DEADLINE_EXCEEDED once a whole `REPLY_STALL_MS` window passes with
   * nothing heard on it (headers, a chunk of a message, the end): bytes that keep arriving, however
   * slowly, are progress and never cut it. Without `watch` it is bounded by the session's liveness
   * alone, as a quiet feed must be.
   */
  readonly call: (
    method: string,
    bytes: Uint8Array,
    receive: (message: Uint8Array) => void,
    watch?: Watch,
  ) => Effect.Effect<void, GrpcFailed>;
  /** A call with exactly one reply, watched to its end. */
  readonly unary: (method: string, bytes: Uint8Array) => Effect.Effect<Uint8Array, GrpcFailed>;
  /** Completes when the session ended: the peer closed it, said goaway, or the scope closed. */
  readonly lost: Effect.Effect<void>;
  /**
   * How many times the peer has sent something on any call (headers, a chunk of a message, the
   * end) since the session opened. It only rises, so two reads that differ prove the link
   * delivered bytes between them; a message that trickles in over a slow link rises with every
   * chunk, which is how a slow transfer is told from a dead link.
   */
  readonly activity: Effect.Effect<number>;
}

const METHOD = /^\/[A-Za-z][\w.]*\/[A-Za-z]\w*$/;

type Stream = ReturnType<ClientHttp2Session['request']>;
type Receive = (message: Uint8Array) => void;

const unavailable = (message: string): GrpcFailed =>
  new GrpcFailed({code: GrpcCode.UNAVAILABLE, message});

/** The metadata every call of a session carries: gRPC metadata is HTTP/2 headers, in lowercase. */
export type CallMetadata = Readonly<Record<string, string>>;

/** Start the HTTP/2 stream of one call, or say why it cannot start. */
const startStream = (
  http2: ClientHttp2Session,
  method: string,
  frame: Uint8Array,
  metadata: CallMetadata,
): Stream | GrpcFailed => {
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
    return unavailable(`gateway session closed: ${String(cause)}`);
  }
};

/**
 * Read one call's stream: reply messages to `receive` in order, then the status the peer ended it
 * with. `finish` is told the failure, or nothing for a call that ended with status OK.
 */
const watchStream = (
  stream: Stream,
  receive: Receive,
  finish: (error?: GrpcFailed) => void,
  settled: () => boolean,
  receiveLimit: number,
  heard: () => void,
): void => {
  let rest: Uint8Array = new Uint8Array(0);
  let status: string | undefined;
  let detail = '';
  const headers = (value: IncomingHttpHeaders): void => {
    heard();
    if (value['grpc-status'] !== undefined) status = String(value['grpc-status']);
    if (value['grpc-message'] !== undefined) detail = String(value['grpc-message']);
    const http = value[':status'];
    if (http !== undefined && String(http) !== '200')
      finish(unavailable(`gRPC HTTP status ${String(http)}`));
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
  stream.on('error', error => finish(unavailable(error.message)));
  stream.on('data', (chunk: Buffer) => {
    heard();
    if (settled()) return;
    const decoded = decodeFrames(rest, chunk, receiveLimit);
    if (isFault(decoded)) return finish(new GrpcFailed(decoded));
    rest = decoded.rest;
    deliver(decoded.messages);
  });
  stream.on('end', () => {
    // A stream that ends with no status was cut, not answered: `callStatus` makes it UNAVAILABLE,
    // which the client redials, rather than a status of the host's own, which it would not.
    if (rest.byteLength !== 0)
      return finish(new GrpcFailed({code: GrpcCode.DATA_LOSS, message: 'truncated gRPC message'}));
    const failure = callStatus(status, detail);
    finish(failure === undefined ? undefined : new GrpcFailed(failure));
  });
  stream.on('close', () => finish(unavailable('gRPC connection closed')));
};

/** What one call has heard and delivered, which its watch reads. */
interface Tally {
  heard: number;
  replied: boolean;
}

/** What a call needs of its session: the connection, what the peer was heard to send, and the metadata of every call. */
interface Line {
  readonly http2: ClientHttp2Session;
  readonly heard: () => void;
  readonly metadata: CallMetadata;
}

/** The send of one call over `line`; its progress is counted in `tally`. */
const startCall = (
  {http2, heard, metadata}: Line,
  tally: Tally,
  method: string,
  bytes: Uint8Array,
  receive: Receive,
): Effect.Effect<void, GrpcFailed> =>
  Effect.callback<void, GrpcFailed>(resume => {
    const frame = encodeFrame(bytes, HOST_API_MAX_MESSAGE_BYTES);
    if (isFault(frame)) return resume(Effect.fail(new GrpcFailed(frame)));
    const stream = startStream(http2, method, frame, metadata);
    if (stream instanceof GrpcFailed) return resume(Effect.fail(stream));
    let settled = false;
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
      () => {
        tally.heard += 1;
        heard();
      },
    );
    return Effect.sync(() => {
      if (settled) return;
      settled = true;
      stream.close(constants.NGHTTP2_CANCEL);
    });
  });

/**
 * Fails when a whole window passes with nothing heard on the call. Never ends otherwise: it is
 * raced against the call, and a watch `to-first-reply` stops watching once the first reply came.
 */
const stalled = (tally: Tally, watch: Watch): Effect.Effect<never, GrpcFailed> =>
  Effect.gen(function* () {
    while (true) {
      const before = tally.heard;
      yield* Effect.sleep(`${REPLY_STALL_MS} millis`);
      if (watch === 'to-first-reply' && tally.replied) return yield* Effect.never;
      if (tally.heard === before)
        return yield* new GrpcFailed({
          code: GrpcCode.DEADLINE_EXCEEDED,
          message: `gRPC deadline exceeded: nothing heard for ${REPLY_STALL_MS / 1000} seconds`,
        });
    }
  });

/** One call over `line`: send `bytes`, then `watchStream`. Interrupting resets the stream. */
const callOver =
  (line: Line): GatewaySession['call'] =>
  (method, bytes, receive, watch) =>
    Effect.suspend(() => {
      const tally: Tally = {heard: 0, replied: false};
      const run = startCall(line, tally, method, bytes, receive);
      return watch === undefined ? run : Effect.raceFirst(run, stalled(tally, watch));
    });

/** A call with exactly one reply, watched to its end. */
const unaryOver =
  (call: GatewaySession['call']): GatewaySession['unary'] =>
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
        'to-end',
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
        return session;
      }),
      session =>
        Effect.sync(() => {
          session.destroy();
          markLost();
        }),
    );
    let heard = 0;
    const call = callOver({
      http2,
      heard: () => {
        heard += 1;
      },
      metadata,
    });
    return {
      call,
      unary: unaryOver(call),
      lost: Deferred.await(lost),
      activity: Effect.sync(() => heard),
    } satisfies GatewaySession;
  });
