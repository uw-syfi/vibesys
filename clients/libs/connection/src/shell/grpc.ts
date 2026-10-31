/** gRPC over one caller-owned HTTP/2 session. No compression; bounded incremental framing. */
import {type ClientHttp2Session, connect, constants, type IncomingHttpHeaders} from 'node:http2';
import type {Duplex} from 'node:stream';
import {HOST_API_MAX_MESSAGE_BYTES as MAX_MESSAGE} from '@vibesys/proto';
import {GrpcCode, GrpcError, registerAbort, type ScheduleTimeout} from '@vibesys/transport';

export function grpcFrame(bytes: Uint8Array): Buffer {
  if (bytes.byteLength > MAX_MESSAGE)
    throw new GrpcError(8, `gRPC message exceeds ${MAX_MESSAGE} bytes`);
  const frame = Buffer.alloc(5 + bytes.byteLength);
  frame.writeUInt32BE(bytes.byteLength, 1);
  frame.set(bytes, 5);
  return frame;
}

export class GrpcDecoder {
  #buffer = Buffer.alloc(0);

  push(bytes: Uint8Array): readonly Uint8Array[] {
    this.#buffer = Buffer.concat([this.#buffer, bytes]);
    const messages: Uint8Array[] = [];
    while (this.#buffer.length >= 5) {
      if (this.#buffer[0] !== 0)
        throw new GrpcError(12, 'compressed gRPC messages are unsupported');
      const size = this.#buffer.readUInt32BE(1);
      if (size > MAX_MESSAGE) throw new GrpcError(8, `gRPC message exceeds ${MAX_MESSAGE} bytes`);
      if (this.#buffer.length < size + 5) break;
      messages.push(this.#buffer.subarray(5, size + 5));
      this.#buffer = this.#buffer.subarray(size + 5);
    }
    return messages;
  }

  finish(): void {
    if (this.#buffer.length !== 0) throw new Error('truncated gRPC message');
  }
}

/** How long a call may take. A unary call has a total only; a stream may also bound its silence. */
export interface CallBound {
  /** The longest the call may run, answered or not. */
  readonly totalMs: number;
  /** The longest wait for the first frame; `silenceMs` when unset. */
  readonly firstFrameMs?: number;
  /** The longest wait between frames, from the first on. Unset: frames may be any distance apart. */
  readonly silenceMs?: number;
}

/** The part of a call's bound that ran out. */
export type TimedOutBound = 'total' | 'first-frame' | 'silence';

/** A call that outlasted its bound: a gRPC deadline that names the part of the bound that ran out. */
export class CallTimedOut extends GrpcError {
  constructor(
    readonly bound: TimedOutBound,
    readonly ms: number,
  ) {
    super(
      GrpcCode.DEADLINE_EXCEEDED,
      bound === 'total'
        ? 'gRPC deadline exceeded'
        : `gRPC deadline exceeded: no ${bound === 'first-frame' ? 'first ' : ''}frame for ${ms / 1000}s`,
    );
    this.name = 'CallTimedOut';
  }
}

/** The session owns its duplex and multiplexes every call for this host. */
export class GatewaySession {
  readonly #session: ClientHttp2Session;

  constructor(
    duplex: Duplex,
    private readonly schedule: ScheduleTimeout,
    lost: () => void = () => {},
  ) {
    this.#session = connect('http://vibesys', {createConnection: () => duplex});
    // Calls receive their own errors; an idle connection must also have an error owner.
    this.#session.on('error', () => {});
    this.#session.once('close', lost);
    this.#session.once('goaway', lost);
  }

  close(): void {
    this.#session.destroy();
  }

  async unary(method: string, bytes: Uint8Array, signal: AbortSignal): Promise<Uint8Array> {
    let response: Uint8Array | undefined;
    await this.stream(
      method,
      bytes,
      signal,
      message => {
        if (response !== undefined) throw new GrpcError(13, 'unary returned multiple messages');
        response = message;
      },
      {totalMs: 15_000},
    );
    if (response === undefined) throw new GrpcError(13, 'unary returned no message');
    return response;
  }

  stream(
    method: string,
    bytes: Uint8Array,
    signal: AbortSignal,
    receive: (bytes: Uint8Array) => void,
    bound?: CallBound,
  ): Promise<void> {
    if (!/^\/[A-Za-z][\w.]*\/[A-Za-z]\w*$/.test(method)) {
      return Promise.reject(new Error(`invalid gRPC method: ${method}`));
    }
    if (signal.aborted) return Promise.reject(signal.reason);
    let frame: Buffer;
    try {
      frame = grpcFrame(bytes);
    } catch (error) {
      return Promise.reject(error);
    }
    return new Promise((resolve, reject) => {
      const stream = this.#session.request({
        ':method': 'POST',
        ':path': method,
        'content-type': 'application/grpc',
        te: 'trailers',
      });
      const decoder = new GrpcDecoder();
      let status: string | undefined;
      let detail = '';
      let settled = false;
      let cancelAbort = (): void => {};
      let cancelDeadline = (): void => {};
      let cancelSilence = (): void => {};
      const finish = (error?: unknown): void => {
        if (settled) return;
        settled = true;
        cancelAbort();
        cancelDeadline();
        cancelSilence();
        if (error !== undefined) {
          stream.close(constants.NGHTTP2_CANCEL);
          reject(error);
        } else resolve();
      };
      const headers = (value: IncomingHttpHeaders): void => {
        if (value['grpc-status'] !== undefined) status = String(value['grpc-status']);
        if (value['grpc-message'] !== undefined) detail = String(value['grpc-message']);
        if (value[':status'] !== undefined && String(value[':status']) !== '200') {
          finish(new Error(`gRPC HTTP status ${value[':status']}`));
        }
      };
      stream.on('response', headers);
      stream.on('trailers', headers);
      stream.on('error', finish);
      // Waiting for a frame: the quiet that may pass before the next one, restarted by each.
      const awaitFrame = (waitMs: number | undefined, bound: TimedOutBound): void => {
        cancelSilence();
        if (waitMs !== undefined)
          cancelSilence = this.schedule(() => finish(new CallTimedOut(bound, waitMs)), waitMs);
      };
      stream.on('data', (chunk: Buffer) => {
        try {
          for (const message of decoder.push(chunk)) {
            receive(message);
            awaitFrame(bound?.silenceMs, 'silence');
          }
        } catch (error) {
          finish(error);
        }
      });
      stream.on('end', () => {
        try {
          decoder.finish();
          if (status !== '0')
            throw new GrpcError(Number(status ?? 2), detail || 'missing gRPC status');
          finish();
        } catch (error) {
          finish(error);
        }
      });
      stream.on('close', () => {
        if (!settled) finish(new Error('gRPC connection closed'));
      });
      const registration = registerAbort(signal, () =>
        finish(signal.reason ?? new Error('cancelled')),
      );
      cancelAbort = registration.detach;
      if (registration.aborted) finish(signal.reason ?? new Error('cancelled'));
      if (!settled && bound !== undefined) {
        const {totalMs} = bound;
        cancelDeadline = this.schedule(() => finish(new CallTimedOut('total', totalMs)), totalMs);
        awaitFrame(bound.firstFrameMs ?? bound.silenceMs, 'first-frame');
      }
      if (!settled) stream.end(frame);
    });
  }
}
