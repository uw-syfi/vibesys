/** gRPC over one caller-owned HTTP/2 session. No compression; bounded incremental framing. */
import {type ClientHttp2Session, connect, constants, type IncomingHttpHeaders} from 'node:http2';
import type {Duplex} from 'node:stream';
import {registerAbort, type ScheduleTimeout} from '@vibesys/transport';

const MAX_MESSAGE = 16 * 1024 * 1024;

export class GrpcError extends Error {
  constructor(
    readonly code: number,
    message: string,
  ) {
    super(message);
    this.name = 'GrpcError';
  }
}

export function grpcFrame(bytes: Uint8Array): Buffer {
  if (bytes.byteLength > MAX_MESSAGE) throw new GrpcError(8, 'gRPC message exceeds 16 MiB');
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
      if (size > MAX_MESSAGE) throw new GrpcError(8, 'gRPC message exceeds 16 MiB');
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
      15_000,
    );
    if (response === undefined) throw new GrpcError(13, 'unary returned no message');
    return response;
  }

  stream(
    method: string,
    bytes: Uint8Array,
    signal: AbortSignal,
    receive: (bytes: Uint8Array) => void,
    deadlineMs?: number,
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
      const finish = (error?: unknown): void => {
        if (settled) return;
        settled = true;
        cancelAbort();
        cancelDeadline();
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
      stream.on('data', (chunk: Buffer) => {
        try {
          for (const message of decoder.push(chunk)) receive(message);
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
      if (!settled && deadlineMs !== undefined) {
        cancelDeadline = this.schedule(
          () => finish(new Error('gRPC deadline exceeded')),
          deadlineMs,
        );
      }
      if (!settled) stream.end(frame);
    });
  }
}
