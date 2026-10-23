/** Generic protobuf bridge. The caller owns every call and cancels it on leaving its screen. */
import {type HostKey, hostFromKey} from '@vibesys/hosts';

export const GATEWAY_CHANNEL = 'vibesys:gateway';
export interface GatewayCall<T> {
  readonly result: Promise<T>;
  cancel(): void;
}
export interface GatewayBridge {
  unary(host: HostKey, method: string, bytes: Uint8Array): GatewayCall<Uint8Array>;
  stream(
    host: HostKey,
    method: string,
    bytes: Uint8Array,
    receive: (bytes: Uint8Array) => void,
    ended: (error?: string, status?: number) => void,
  ): {cancel(): void};
}
/**
 * What the preload's unary resolves with. An `Error`'s own properties do not cross the context
 * bridge (only its message and stack do), so a refusal travels as a value carrying its gRPC
 * status, and `gatewayBridge` turns it back into a `GatewayError` in the page.
 */
export type GatewayUnaryReply =
  | {readonly bytes: Uint8Array}
  | {readonly error: string; readonly status?: number};
/** The bridge the preload exposes: values only across the context bridge. */
export interface GatewayPreload {
  unary(host: HostKey, method: string, bytes: Uint8Array): GatewayCall<GatewayUnaryReply>;
  stream: GatewayBridge['stream'];
}
/** A call the gateway or the link failed; `status` is the gRPC code when the gateway refused it. */
export class GatewayError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
  }
}
/** The page's bridge over the preload's: a refused unary rejects with a `GatewayError`. */
export function gatewayBridge(preload: GatewayPreload): GatewayBridge {
  return {
    unary(host, method, bytes) {
      const call = preload.unary(host, method, bytes);
      return {
        result: call.result.then(reply => {
          if ('bytes' in reply) return reply.bytes;
          throw new GatewayError(reply.error, reply.status);
        }),
        cancel: () => call.cancel(),
      };
    },
    stream: (host, method, bytes, receive, ended) =>
      preload.stream(host, method, bytes, receive, ended),
  };
}
export interface GatewayRequest {
  readonly kind: 'unary' | 'stream';
  readonly host: HostKey;
  readonly method: string;
  readonly bytes: Uint8Array;
}
export type GatewayReply =
  | {readonly bytes: Uint8Array}
  | {readonly end: true; readonly error?: string; readonly status?: number};

export function parseGatewayRequest(value: unknown): GatewayRequest {
  if (value === null || typeof value !== 'object')
    throw new Error('gateway request must be an object');
  const record = value as Record<string, unknown>;
  for (const key of Object.keys(record)) {
    if (!['kind', 'host', 'method', 'bytes'].includes(key))
      throw new Error(`unknown gateway key: ${key}`);
  }
  const {kind, host, method, bytes} = record;
  hostFromKey(host);
  if (kind !== 'unary' && kind !== 'stream')
    throw new Error('gateway kind must be unary or stream');
  if (typeof method !== 'string' || !/^\/[A-Za-z][\w.]*\/[A-Za-z]\w*$/.test(method))
    throw new Error('invalid gateway method');
  if (!(bytes instanceof Uint8Array) || bytes.byteLength > 16 * 1024 * 1024)
    throw new Error('gateway bytes must be at most 16 MiB');
  return {kind, host: host as HostKey, method, bytes};
}
