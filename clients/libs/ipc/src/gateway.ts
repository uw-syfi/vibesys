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
    ended: (error?: string) => void,
  ): {cancel(): void};
}
export interface GatewayRequest {
  readonly kind: 'unary' | 'stream';
  readonly host: HostKey;
  readonly method: string;
  readonly bytes: Uint8Array;
}
export type GatewayReply =
  | {readonly bytes: Uint8Array}
  | {readonly end: true; readonly error?: string};

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
