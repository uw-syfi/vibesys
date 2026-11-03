/**
 * The messages the window and the main process exchange over one MessagePort per call: a
 * `DesktopGatewayRequest` going out, `DesktopGatewayReply` frames coming back, the last of them an
 * end. They are the generated protobuf messages of `proto/vibesys/desktop/v1/gateway.proto`,
 * validated on both sides; no routing or status crosses Electron outside them.
 */
import {create, toBinary} from '@bufbuild/protobuf';
import {
  DesktopGatewayKind,
  DesktopGatewayReplySchema,
  DesktopGatewayRequestSchema,
  validatedExactFromBinary,
} from '@vibesys/proto';
import type {HostKey} from '../state/host.js';
import {hostFromKey} from '../state/host.js';

/** The IPC channel the main process serves; the preload repeats it (a test holds the two equal). */
export const GATEWAY_CHANNEL = 'vibesys:gateway';

/** What the preload exposes on the window: bytes in, bytes out, one port per call. */
export interface GatewayPortBridge {
  open(
    request: Uint8Array,
    receive: (reply: Uint8Array) => void,
    failed: (error: string) => void,
  ): {cancel(): void};
}

/** A decoded request: which host, which method, and the message bytes. */
export interface GatewayRequest {
  readonly kind: 'unary' | 'stream';
  readonly host: HostKey;
  readonly method: string;
  readonly bytes: Uint8Array;
}

export function encodeGatewayRequest(request: GatewayRequest): Uint8Array {
  hostFromKey(request.host);
  const kind = request.kind === 'unary' ? DesktopGatewayKind.UNARY : DesktopGatewayKind.STREAM;
  const bytes = toBinary(
    DesktopGatewayRequestSchema,
    create(DesktopGatewayRequestSchema, {...request, kind}),
  );
  validatedExactFromBinary(DesktopGatewayRequestSchema, bytes);
  return bytes;
}

/** The request a window sent; anything that is not a valid encoded request is an error. */
export function parseGatewayRequest(value: unknown): GatewayRequest {
  if (!(value instanceof Uint8Array)) throw new Error('gateway request must be protobuf bytes');
  const message = validatedExactFromBinary(DesktopGatewayRequestSchema, value);
  return {
    kind: message.kind === DesktopGatewayKind.UNARY ? 'unary' : 'stream',
    host: message.host,
    method: message.method,
    bytes: message.bytes,
  };
}

/** One reply frame. */
export function gatewayReply(bytes: Uint8Array): Uint8Array {
  const reply = toBinary(
    DesktopGatewayReplySchema,
    create(DesktopGatewayReplySchema, {body: {case: 'bytes', value: bytes}}),
  );
  validatedExactFromBinary(DesktopGatewayReplySchema, reply);
  return reply;
}

/** The last reply of a call: clean without `error` and `status`, else the failure and its gRPC status. */
export function gatewayEnd(error?: string, status?: number): Uint8Array {
  const reply = toBinary(
    DesktopGatewayReplySchema,
    create(DesktopGatewayReplySchema, {
      body: {
        case: 'end',
        value: {
          ...(error === undefined ? {} : {error}),
          ...(status === undefined ? {} : {status}),
        },
      },
    }),
  );
  validatedExactFromBinary(DesktopGatewayReplySchema, reply);
  return reply;
}

export function decodeGatewayReply(bytes: Uint8Array) {
  return validatedExactFromBinary(DesktopGatewayReplySchema, bytes).body;
}
