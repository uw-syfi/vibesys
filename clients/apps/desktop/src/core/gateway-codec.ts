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
  type DesktopLinkProblem,
  type DesktopRefusal,
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

/**
 * `DesktopGatewayEnd.error`'s wire limit (`proto/vibesys/desktop/v1/gateway.proto`). A failure's
 * message is not bounded at its source (a schema violation can list every broken field of a large
 * reply), so `gatewayEnd` must bound it itself: otherwise a long enough failure could never be
 * reported, because building its own end reply is what would fail.
 */
const MAX_ERROR_BYTES = 65536;

/** How many UTF-8 bytes one Unicode code point takes. */
function utf8Width(codePoint: number): number {
  if (codePoint < 0x80) return 1;
  if (codePoint < 0x800) return 2;
  if (codePoint < 0x10000) return 3;
  return 4;
}

/**
 * `value` cut to at most `limit` UTF-8 bytes, never splitting a character (`core` has no text
 * encoder of its own, so this walks code points instead of measuring encoded bytes directly).
 */
function boundedUtf8(value: string, limit: number): string {
  let bytes = 0;
  let end = 0;
  for (const char of value) {
    bytes += utf8Width(char.codePointAt(0) ?? 0);
    if (bytes > limit) return value.slice(0, end);
    end += char.length;
  }
  return value;
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

/**
 * The last reply of a call: clean without `error` and `status`, else the failure and its gRPC
 * status, the link problem that ended it, or the refusal that no retry changes.
 */
export function gatewayEnd(
  error?: string,
  status?: number,
  linkProblem?: DesktopLinkProblem,
  refusal?: DesktopRefusal,
): Uint8Array {
  const reply = toBinary(
    DesktopGatewayReplySchema,
    create(DesktopGatewayReplySchema, {
      body: {
        case: 'end',
        value: {
          ...(error === undefined ? {} : {error: boundedUtf8(error, MAX_ERROR_BYTES)}),
          ...(status === undefined ? {} : {status}),
          ...(linkProblem === undefined ? {} : {linkProblem}),
          ...(refusal === undefined ? {} : {refusal}),
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
