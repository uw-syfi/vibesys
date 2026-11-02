import {describe, expect, test} from 'bun:test';
import fc from 'fast-check';
import {encodeFrame, FrameDecoder} from './frames.js';

const frames = fc.array(
  fc.oneof(
    fc.record({id: fc.nat(), response: fc.jsonValue()}),
    fc.record({id: fc.nat(), end: fc.constant(true)}),
    fc.record({id: fc.nat(), error: fc.record({kind: fc.string(), message: fc.string()})}),
  ),
  {maxLength: 12},
);

function cut(bytes: Uint8Array, points: readonly number[]): Uint8Array[] {
  const sorted = [...new Set(points.map(point => point % (bytes.length + 1)))].sort(
    (a, b) => a - b,
  );
  const pieces: Uint8Array[] = [];
  let from = 0;
  for (const point of [...sorted, bytes.length]) {
    pieces.push(bytes.slice(from, point));
    from = point;
  }
  return pieces;
}

describe('frames', () => {
  test('any chunking of a byte stream decodes to the same frames', () => {
    fc.assert(
      fc.property(frames, fc.array(fc.nat(), {maxLength: 20}), (sent, points) => {
        const parts = sent.map(frame => encodeFrame(frame));
        const bytes = new Uint8Array(parts.reduce((total, part) => total + part.length, 0));
        let offset = 0;
        for (const part of parts) {
          bytes.set(part, offset);
          offset += part.length;
        }
        const decoder = new FrameDecoder();
        const got = cut(bytes, points).flatMap(chunk => decoder.push(chunk));
        expect(got).toEqual(JSON.parse(JSON.stringify(sent)));
      }),
    );
  });

  test('arbitrary bytes either decode or throw an Error, never hang or throw anything else', () => {
    fc.assert(
      fc.property(fc.uint8Array({maxLength: 200}), bytes => {
        try {
          new FrameDecoder().push(bytes);
        } catch (error) {
          expect(error).toBeInstanceOf(Error);
        }
      }),
      {numRuns: 500},
    );
  });
});
