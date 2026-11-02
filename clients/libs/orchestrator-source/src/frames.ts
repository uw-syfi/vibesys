/**
 * The framing of `vibesys-orchestrator connect` on stdio: a 4-byte big-endian length, then that
 * many bytes of one UTF-8 JSON envelope. Requests carry a client-chosen call id; the daemon
 * answers with `response` frames for that id and then `end`, or with one `error`.
 *
 *   client to daemon: {"id": 1, "method": "WatchRuns", "request": <protobuf JSON>}
 *                     {"id": 1, "cancel": true}
 *   daemon to client: {"id": 1, "response": <protobuf JSON>} ... {"id": 1, "end": true}
 *                     {"id": 1, "error": {"kind": "refused", "message": "..."}}
 *
 * The daemon item has not fixed this framing yet; it is one decision made here, in one module, so
 * that moving to the daemon's is a change of this file.
 */
export type ClientFrame =
  | {readonly id: number; readonly method: string; readonly request: unknown}
  | {readonly id: number; readonly cancel: true};

export type DaemonFrame =
  | {readonly id: number; readonly response: unknown}
  | {readonly id: number; readonly end: true}
  | {readonly id: number; readonly error: {readonly kind: string; readonly message: string}};

const MAX_FRAME = 64 * 1024 * 1024;

export function encodeFrame(frame: ClientFrame | DaemonFrame): Uint8Array {
  const body = new TextEncoder().encode(JSON.stringify(frame));
  const out = new Uint8Array(4 + body.length);
  new DataView(out.buffer).setUint32(0, body.length);
  out.set(body, 4);
  return out;
}

/** Cuts a byte stream, given in any chunks, into frames. A bad length or body throws. */
export class FrameDecoder {
  #buffer = new Uint8Array(0);

  push(chunk: Uint8Array): unknown[] {
    const joined = new Uint8Array(this.#buffer.length + chunk.length);
    joined.set(this.#buffer);
    joined.set(chunk, this.#buffer.length);
    const frames: unknown[] = [];
    let offset = 0;
    while (joined.length - offset >= 4) {
      const length = new DataView(joined.buffer, joined.byteOffset + offset).getUint32(0);
      if (length > MAX_FRAME) throw new Error(`frame of ${length} bytes is over the limit`);
      if (joined.length - offset - 4 < length) break;
      const text = new TextDecoder('utf-8', {fatal: true}).decode(
        joined.subarray(offset + 4, offset + 4 + length),
      );
      frames.push(JSON.parse(text));
      offset += 4 + length;
    }
    this.#buffer = joined.slice(offset);
    return frames;
  }
}
