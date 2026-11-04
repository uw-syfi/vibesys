import {describe, expect, it} from '@effect/vitest';
import {ORCHESTRATOR_MAX_MESSAGE_BYTES} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {type Decoded, decodeFrames, encodeFrame, type FrameFault, isFault} from './grpc-frames.js';

const LIMIT = 1024;
const message = fc.uint8Array({maxLength: 200});

const framed = (messages: readonly Uint8Array[]): Uint8Array => {
  const frames = messages.map(bytes => {
    const frame = encodeFrame(bytes, LIMIT);
    if (isFault(frame)) throw new Error(frame.message);
    return frame as Uint8Array;
  });
  const all = new Uint8Array(frames.reduce((sum, frame) => sum + frame.length, 0));
  let offset = 0;
  for (const frame of frames) {
    all.set(frame, offset);
    offset += frame.length;
  }
  return all;
};

/** Feed `bytes` in the chunks `cuts` choose and collect what the decoder yields. */
const feed = (bytes: Uint8Array, cuts: readonly number[]): Decoded | FrameFault => {
  let rest: Uint8Array = new Uint8Array(0);
  const messages: Uint8Array[] = [];
  let at = 0;
  for (const cut of [
    ...cuts.map(c => c % (bytes.length + 1)).sort((a, b) => a - b),
    bytes.length,
  ]) {
    const result = decodeFrames(rest, bytes.subarray(at, cut), LIMIT);
    if (isFault(result)) return result;
    messages.push(...(result as Decoded).messages);
    rest = (result as Decoded).rest;
    at = Math.max(at, cut);
  }
  return {messages, rest};
};

describe('gRPC framing', () => {
  it('accepts exactly 4 MiB and refuses one byte more in the shared decoder', () => {
    const limit = ORCHESTRATOR_MAX_MESSAGE_BYTES;
    const frame = (size: number): Uint8Array => {
      const bytes = new Uint8Array(5 + size);
      new DataView(bytes.buffer).setUint32(1, size);
      return bytes;
    };
    const result = decodeFrames(new Uint8Array(0), frame(limit), limit);
    expect(isFault(result)).toBe(false);
    expect((result as Decoded).messages.map(message => message.length)).toEqual([limit]);
    expect((result as Decoded).rest.length).toBe(0);
    expect(decodeFrames(new Uint8Array(0), frame(limit + 1), limit)).toMatchObject({code: 8});
  });

  it('reads back the messages written, however the bytes are cut into chunks', () => {
    fc.assert(
      fc.property(
        fc.array(message, {maxLength: 6}),
        fc.array(fc.nat(2000), {maxLength: 8}),
        (messages, cuts) => {
          const result = feed(framed(messages), cuts);
          expect(isFault(result)).toBe(false);
          expect((result as Decoded).messages).toEqual(messages);
          expect((result as Decoded).rest.length).toBe(0);
        },
      ),
    );
  });

  it('keeps an unfinished message as the rest and yields nothing for it', () => {
    fc.assert(
      fc.property(message, fc.nat(), (bytes, n) => {
        const all = framed([bytes]);
        const cut = n % all.length;
        const result = decodeFrames(new Uint8Array(0), all.subarray(0, cut), LIMIT) as Decoded;
        expect(result.messages).toEqual([]);
        expect(result.rest.length).toBe(cut);
      }),
    );
  });

  it('faults on a message above the limit, writing or reading, and on a compressed one', () => {
    expect(encodeFrame(new Uint8Array(LIMIT + 1), LIMIT)).toMatchObject({code: 8});
    const header = new Uint8Array([0, 0, 0, 0xff, 0xff]);
    expect(decodeFrames(new Uint8Array(0), header, LIMIT)).toMatchObject({code: 8});
    expect(decodeFrames(new Uint8Array(0), new Uint8Array([1, 0, 0, 0, 0]), LIMIT)).toMatchObject({
      code: 12,
    });
  });
});
