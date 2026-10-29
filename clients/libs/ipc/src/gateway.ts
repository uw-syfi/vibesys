/** Protobuf-only Electron messages; the preload owns ports and the caller owns calls. */
import {create, toBinary} from '@bufbuild/protobuf';
import {type HostKey, hostFromKey} from '@vibesys/hosts';
import {
  DesktopGatewayKind,
  DesktopGatewayReplySchema,
  DesktopGatewayRequestSchema,
  validatedExactFromBinary,
} from '@vibesys/proto';

export const GATEWAY_CHANNEL = 'vibesys:gateway';
export interface GatewayCall<T> {
  readonly result: Promise<T>;
  cancel(): void;
}
/** The app-facing capability, composed in the renderer over the byte-only preload. */
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
/** A call the gateway or the link failed; `status` is the gRPC code when the gateway refused it. */
export class GatewayError extends Error {
  constructor(
    message: string,
    readonly status?: number,
  ) {
    super(message);
  }
}
/** No routing or response metadata crosses Electron outside a generated message. */
export interface GatewayPortBridge {
  open(
    request: Uint8Array,
    receive: (reply: Uint8Array) => void,
    failed: (error: string) => void,
  ): {cancel(): void};
}
/** Decoded routing projected for the main connection owner; never sent as an object. */
export interface GatewayRequest {
  readonly kind: 'unary' | 'stream';
  readonly host: HostKey;
  readonly method: string;
  readonly bytes: Uint8Array;
}

export function encodeGatewayRequest(request: GatewayRequest): Uint8Array {
  for (const key of Object.keys(request)) {
    if (!DesktopGatewayRequestSchema.fields.some(field => field.localName === key))
      throw new Error(`unknown gateway key: ${key}`);
  }
  hostFromKey(request.host);
  const kind =
    request.kind === 'unary'
      ? DesktopGatewayKind.UNARY
      : request.kind === 'stream'
        ? DesktopGatewayKind.STREAM
        : DesktopGatewayKind.UNSPECIFIED;
  const message = create(DesktopGatewayRequestSchema, {...request, kind});
  const bytes = toBinary(DesktopGatewayRequestSchema, message);
  validatedExactFromBinary(DesktopGatewayRequestSchema, bytes);
  return bytes;
}

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

export function gatewayReply(bytes: Uint8Array): Uint8Array {
  const reply = toBinary(
    DesktopGatewayReplySchema,
    create(DesktopGatewayReplySchema, {body: {case: 'bytes', value: bytes}}),
  );
  validatedExactFromBinary(DesktopGatewayReplySchema, reply);
  return reply;
}
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

/** Validate before opening a port and retire that port exactly once on every exit. */
export function gatewayBridge(ports: GatewayPortBridge): GatewayBridge {
  function open(
    request: GatewayRequest,
    receive: (bytes: Uint8Array) => void,
    ended: (error?: string, status?: number) => void,
  ) {
    const encoded = encodeGatewayRequest(request);
    let port: {cancel(): void} | undefined;
    let done = false;
    const finish = (error?: string, status?: number): void => {
      if (done) return;
      done = true;
      port?.cancel();
      ended(error, status);
    };
    try {
      port = ports.open(
        encoded,
        bytes => {
          if (done) return;
          try {
            const reply = decodeGatewayReply(bytes);
            if (reply.case === 'bytes') receive(reply.value);
            else if (reply.case === 'end') finish(reply.value.error, reply.value.status);
          } catch (error) {
            finish(String(error));
          }
        },
        finish,
      );
      if (done) port.cancel();
    } catch (error) {
      finish(String(error));
    }
    return {cancel: () => finish('cancelled')};
  }
  return {
    unary(host, method, bytes) {
      let cancel = (): void => {};
      const result = new Promise<Uint8Array>((resolve, reject) => {
        let response: Uint8Array | undefined;
        const call = open(
          {kind: 'unary', host, method, bytes},
          value => {
            if (response !== undefined)
              throw new Error('unary gateway returned multiple responses');
            response = value;
          },
          (error, status) => {
            if (error !== undefined) reject(new GatewayError(error, status));
            else if (response === undefined) reject(new Error('missing unary response'));
            else resolve(response);
          },
        );
        cancel = call.cancel;
      });
      return {result, cancel: () => cancel()};
    },
    stream(host, method, bytes, receive, ended) {
      return open({kind: 'stream', host, method, bytes}, receive, ended);
    },
  };
}
