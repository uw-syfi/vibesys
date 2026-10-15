const INITIAL = new Uint32Array([
  0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
]);

const ROUND = new Uint32Array([
  0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
  0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
  0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
  0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
  0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
  0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
  0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
  0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
]);

function rotateRight(value: number, bits: number): number {
  return (value >>> bits) | (value << (32 - bits));
}

function word(values: Uint32Array, index: number): number {
  return values[index] as number;
}

function byte(values: Uint8Array, index: number): number {
  return values[index] as number;
}

function utf8CodePoint(scalar: string): number {
  const point = scalar.codePointAt(0) ?? 0;
  // WHATWG TextEncoder and Node's UTF-8 encoder replace unpaired UTF-16
  // surrogates with U+FFFD. A valid pair is one scalar in `for...of` and has a
  // code point above this range.
  return point >= 0xd800 && point <= 0xdfff ? 0xfffd : point;
}

/** UTF-8 byte length without allocating a second copy of an assembled checkpoint. */
export function utf8ByteLength(value: string): number {
  let length = 0;
  for (const scalar of value) {
    const point = utf8CodePoint(scalar);
    length += point < 0x80 ? 1 : point < 0x800 ? 2 : point < 0x10000 ? 3 : 4;
  }
  return length;
}

/**
 * SHA-256 of a JavaScript string's UTF-8 encoding.
 *
 * This package also runs in browsers, so it cannot depend on `node:crypto`,
 * and stream delivery is synchronous, so Web Crypto's promise API is not an
 * ownership fit. The implementation consumes one 64-byte block at a time and
 * never materializes another checkpoint-sized byte array.
 */
export function sha256Utf8(value: string): string {
  const state = new Uint32Array(INITIAL);
  const block = new Uint8Array(64);
  const words = new Uint32Array(64);
  let offset = 0;
  let byteLength = 0;

  const compress = (): void => {
    for (let index = 0; index < 16; index += 1) {
      const start = index * 4;
      words[index] =
        (byte(block, start) << 24) |
        (byte(block, start + 1) << 16) |
        (byte(block, start + 2) << 8) |
        byte(block, start + 3);
    }
    for (let index = 16; index < 64; index += 1) {
      const previous = word(words, index - 15);
      const earlier = word(words, index - 2);
      const sigma0 = rotateRight(previous, 7) ^ rotateRight(previous, 18) ^ (previous >>> 3);
      const sigma1 = rotateRight(earlier, 17) ^ rotateRight(earlier, 19) ^ (earlier >>> 10);
      words[index] = (word(words, index - 16) + sigma0 + word(words, index - 7) + sigma1) >>> 0;
    }

    let a = word(state, 0);
    let b = word(state, 1);
    let c = word(state, 2);
    let d = word(state, 3);
    let e = word(state, 4);
    let f = word(state, 5);
    let g = word(state, 6);
    let h = word(state, 7);
    for (let index = 0; index < 64; index += 1) {
      const sum1 = rotateRight(e, 6) ^ rotateRight(e, 11) ^ rotateRight(e, 25);
      const choose = (e & f) ^ (~e & g);
      const temporary1 = (h + sum1 + choose + word(ROUND, index) + word(words, index)) >>> 0;
      const sum0 = rotateRight(a, 2) ^ rotateRight(a, 13) ^ rotateRight(a, 22);
      const majority = (a & b) ^ (a & c) ^ (b & c);
      const temporary2 = (sum0 + majority) >>> 0;
      h = g;
      g = f;
      f = e;
      e = (d + temporary1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (temporary1 + temporary2) >>> 0;
    }
    state[0] = (word(state, 0) + a) >>> 0;
    state[1] = (word(state, 1) + b) >>> 0;
    state[2] = (word(state, 2) + c) >>> 0;
    state[3] = (word(state, 3) + d) >>> 0;
    state[4] = (word(state, 4) + e) >>> 0;
    state[5] = (word(state, 5) + f) >>> 0;
    state[6] = (word(state, 6) + g) >>> 0;
    state[7] = (word(state, 7) + h) >>> 0;
    offset = 0;
  };

  const append = (byte: number): void => {
    block[offset] = byte;
    offset += 1;
    if (offset === block.length) compress();
  };
  const appendScalar = (point: number): void => {
    if (point < 0x80) append(point);
    else if (point < 0x800) {
      append(0xc0 | (point >>> 6));
      append(0x80 | (point & 0x3f));
    } else if (point < 0x10000) {
      append(0xe0 | (point >>> 12));
      append(0x80 | ((point >>> 6) & 0x3f));
      append(0x80 | (point & 0x3f));
    } else {
      append(0xf0 | (point >>> 18));
      append(0x80 | ((point >>> 12) & 0x3f));
      append(0x80 | ((point >>> 6) & 0x3f));
      append(0x80 | (point & 0x3f));
    }
  };

  for (const scalar of value) {
    const point = utf8CodePoint(scalar);
    byteLength += point < 0x80 ? 1 : point < 0x800 ? 2 : point < 0x10000 ? 3 : 4;
    appendScalar(point);
  }
  append(0x80);
  while (offset !== 56) append(0);
  const highBits = Math.floor(byteLength / 0x20000000);
  const lowBits = (byteLength << 3) >>> 0;
  for (let shift = 24; shift >= 0; shift -= 8) append((highBits >>> shift) & 0xff);
  for (let shift = 24; shift >= 0; shift -= 8) append((lowBits >>> shift) & 0xff);

  return [...state].map(word => word.toString(16).padStart(8, '0')).join('');
}
