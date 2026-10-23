/** Socket-free implementation of the session role. The peer can send only wire data and trailers. */
import type {Duplex} from 'node:stream';
import {registerAbort, type ScheduleTimeout} from '@vibesys/transport';
import {type GatewaySession, GrpcDecoder, GrpcError, grpcFrame, grpcStatus} from '../shell/grpc.js';
import type {Schedule} from './schedule.js';

export interface GatewayWireCall {
  readonly method: string;
  readonly bytes: Uint8Array;
  readonly signal: AbortSignal;
  message(bytes: Uint8Array): void;
  /** The closing trailers; no status is a peer that ended the stream without one. */
  trailers(status: string | undefined, message: string): void;
  close(): void;
}

export class FakeGatewaySession implements GatewaySession {
  readonly #calls = new Set<() => void>();
  #closed = false;
  constructor(
    private readonly duplex: Duplex,
    private readonly schedule: Schedule,
    private readonly request: (call: GatewayWireCall) => void,
    private readonly timers: ScheduleTimeout,
    private readonly lost: () => void = () => {},
  ) {}

  /** Destroy the session as the real one does: calls fail, the duplex goes, `lost` is told. */
  close(): void {
    if (this.#closed) return;
    this.#closed = true;
    for (const close of [...this.#calls]) close();
    this.duplex.destroy();
    // The real session reports its close on a later turn, never inside close().
    this.schedule.post(this, this.lost);
  }

  /** The link drops under the session: the same as a close nobody asked for. */
  drop(): void {
    this.close();
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

  /** Refuses what `Http2GatewaySession` refuses before a byte is sent, with the same errors. */
  stream(
    method: string,
    bytes: Uint8Array,
    signal: AbortSignal,
    receive: (bytes: Uint8Array) => void,
    deadlineMs?: number,
  ): Promise<void> {
    if (!/^\/[A-Za-z][\w.]*\/[A-Za-z]\w*$/.test(method))
      return Promise.reject(new Error(`invalid gRPC method: ${method}`));
    if (this.#closed) return Promise.reject(new Error('The session has been destroyed'));
    if (signal.aborted) return Promise.reject(signal.reason);
    try {
      grpcFrame(bytes);
    } catch (error) {
      return Promise.reject(error);
    }
    return new Promise((resolve, reject) => {
      const lane = {};
      const decoder = new GrpcDecoder();
      let done = false;
      let detach = () => {};
      let cancelDeadline = () => {};
      const close = () => finish(new Error('gRPC connection closed'));
      const finish = (error?: unknown) => {
        if (done) return;
        done = true;
        detach();
        cancelDeadline();
        this.#calls.delete(close);
        if (error !== undefined) reject(error);
        else resolve();
      };
      const consume = (chunk: Uint8Array): void => {
        if (done) return;
        try {
          for (const value of decoder.push(chunk)) receive(value);
        } catch (error) {
          finish(error);
        }
      };
      this.#calls.add(close);
      const registration = registerAbort(signal, () =>
        finish(signal.reason ?? new Error('cancelled')),
      );
      detach = registration.detach;
      if (registration.aborted) {
        finish(signal.reason ?? new Error('cancelled'));
        return;
      }
      if (deadlineMs !== undefined)
        cancelDeadline = this.timers(() => finish(new Error('gRPC deadline exceeded')), deadlineMs);
      this.schedule.post(lane, () => {
        if (done) return;
        this.request({
          method,
          bytes: bytes.slice(),
          signal,
          message: payload => {
            const frame = grpcFrame(payload);
            for (let offset = 0; offset < frame.length; ) {
              const size = 1 + this.schedule.random.int(frame.length - offset);
              const chunk = frame.subarray(offset, offset + size);
              offset += size;
              this.schedule.post(lane, () => {
                consume(chunk);
              });
            }
          },
          trailers: (status, message) =>
            this.schedule.post(lane, () => {
              if (done) return;
              try {
                decoder.finish();
                finish(grpcStatus(status, message));
              } catch (error) {
                finish(error);
              }
            }),
          close: () => this.schedule.post(lane, close),
        });
      });
    });
  }
}
