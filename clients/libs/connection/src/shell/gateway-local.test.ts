import {afterEach, beforeEach, describe, expect, test} from 'bun:test';
import {mkdtemp, readdir, readFile, rm, stat} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {installGatewayBinary} from './gateway-local.js';

let directory = '';
beforeEach(async () => {
  directory = await mkdtemp(join(tmpdir(), 'vsg-local-'));
});
afterEach(async () => {
  await rm(directory, {recursive: true, force: true});
});

describe('installing the local gateway binary', () => {
  test('a binary already holding the bytes is left as the same file, however often it is installed', async () => {
    for (const seed of SEEDS.slice(0, 30)) {
      const random = new Seeded(seed);
      const builds = [0, 1].map(() =>
        Uint8Array.from({length: 1 + random.int(64)}, () => random.int(256)),
      );
      const binary = join(directory, `vibesysd-${seed}`);
      let file: {ino: number; mtimeMs: number} | undefined;
      let expectedReplacements = 0;
      let last: Uint8Array | undefined;
      for (let step = 0; step < 6; step += 1) {
        const bytes = builds[random.int(2)] as Uint8Array;
        await installGatewayBinary(binary, bytes);
        const now = await stat(binary);
        const changed = last === undefined || Buffer.compare(last, bytes) !== 0;
        if (changed) expectedReplacements += 1;
        else expect({ino: now.ino, mtimeMs: now.mtimeMs}).toEqual(file ?? {ino: 0, mtimeMs: 0});
        expect(Buffer.compare(await readFile(binary), bytes)).toBe(0);
        expect(now.mode & 0o777).toBe(0o700);
        file = {ino: now.ino, mtimeMs: now.mtimeMs};
        last = bytes;
      }
      expect(expectedReplacements).toBeGreaterThan(0);
      // Only the binary stays: no temporary file is left beside it.
      expect((await readdir(directory)).filter(name => name.startsWith('.vibesysd-'))).toEqual([]);
    }
  });
});
