/**
 * gRPC message framing (https://github.com/grpc/grpc/blob/master/doc/PROTOCOL-HTTP2.md): a
 * message is a zero compression flag, its length as four big-endian bytes, then the bytes. No
 * compression; every message is bounded by `limit`, so a corrupt length cannot make a reader hold
 * more than that.
 */

/** A framing fault with the gRPC status it ends a call with. */
export interface FrameFault {
  readonly code: number;
  readonly message: string;
}

const RESOURCE_EXHAUSTED = 8;
const UNIMPLEMENTED = 12;
const HEADER = 5;

/** `bytes` as one frame, or the fault when it exceeds `limit`. */
export function encodeFrame(bytes: Uint8Array, limit: number): Uint8Array | FrameFault {
  if (bytes.byteLength > limit)
    return {code: RESOURCE_EXHAUSTED, message: `gRPC message exceeds ${limit} bytes`};
  const frame = new Uint8Array(HEADER + bytes.byteLength);
  new DataView(frame.buffer).setUint32(1, bytes.byteLength);
  frame.set(bytes, HEADER);
  return frame;
}

/** What reading a chunk found: the messages it completed and the unfinished part to carry on with. */
export interface Decoded {
  readonly messages: readonly Uint8Array[];
  readonly rest: Uint8Array;
}

/**
 * The messages `chunk` completes after `rest`, and what remains unfinished; or the fault when the
 * stream is compressed or declares a message above `limit`.
 */
export function decodeFrames(
  rest: Uint8Array,
  chunk: Uint8Array,
  limit: number,
): Decoded | FrameFault {
  let buffer = new Uint8Array(rest.byteLength + chunk.byteLength);
  buffer.set(rest);
  buffer.set(chunk, rest.byteLength);
  const messages: Uint8Array[] = [];
  while (buffer.byteLength >= HEADER) {
    if (buffer[0] !== 0)
      return {code: UNIMPLEMENTED, message: 'compressed gRPC messages are unsupported'};
    const size = new DataView(buffer.buffer, buffer.byteOffset).getUint32(1);
    if (size > limit)
      return {code: RESOURCE_EXHAUSTED, message: `gRPC message exceeds ${limit} bytes`};
    if (buffer.byteLength < size + HEADER) break;
    messages.push(buffer.subarray(HEADER, size + HEADER));
    buffer = buffer.subarray(size + HEADER);
  }
  return {messages, rest: buffer};
}

export const isFault = (value: Decoded | Uint8Array | FrameFault): value is FrameFault =>
  'code' in value;
