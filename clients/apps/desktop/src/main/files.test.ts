import {describe, expect, it} from '@effect/vitest';
import * as Effect from 'effect/Effect';
import fc from 'fast-check';
import {type FileOps, makeWriteFile} from './files.js';

/** A directory in memory. Every staging step yields, so concurrent writers interleave. */
const directory = () => {
  const files = new Map<string, Uint8Array>();
  const staged: string[] = [];
  const ops: FileOps = {
    mkdir: () => Promise.resolve(),
    writeFile: async (path, bytes) => {
      // The real write is exclusive: it fails when the staging file already exists.
      if (files.has(path)) throw new Error(`EEXIST: ${path}`);
      staged.push(path);
      files.set(path, bytes);
      await Promise.resolve();
    },
    rename: async (from, to) => {
      await Promise.resolve();
      const bytes = files.get(from);
      if (bytes === undefined) throw new Error(`ENOENT: ${from}`);
      files.delete(from);
      files.set(to, bytes);
    },
    remove: path => {
      files.delete(path);
      return Promise.resolve();
    },
  };
  return {files, staged, ops};
};

describe('an atomic file write', () => {
  it.effect(
    'stages every concurrent write apart and leaves one whole write behind, with no staging file',
    () =>
      Effect.gen(function* () {
        const batches = fc.sample(
          fc.array(fc.uint8Array({minLength: 1, maxLength: 8}), {minLength: 2, maxLength: 6}),
          {numRuns: 25, seed: 5},
        );
        for (const contents of batches) {
          const {files, staged, ops} = directory();
          let counter = 0;
          const write = makeWriteFile(ops, () => {
            counter += 1;
            return String(counter);
          });
          yield* Effect.all(
            contents.map(bytes => write('/data/preferences.pb', bytes)),
            {concurrency: 'unbounded'},
          );
          expect(new Set(staged).size).toBe(contents.length);
          expect([...files.keys()]).toEqual(['/data/preferences.pb']);
          expect(contents).toContainEqual(files.get('/data/preferences.pb'));
        }
      }),
  );

  it.effect('removes its staging file when the rename fails, and reports the failure', () =>
    Effect.gen(function* () {
      const {files, ops} = directory();
      const write = makeWriteFile(
        {...ops, rename: () => Promise.reject(new Error('EXDEV'))},
        () => 'a',
      );
      const error = yield* Effect.flip(write('/data/preferences.pb', Uint8Array.of(1)));
      expect(error.message).toBe('EXDEV');
      expect(files.size).toBe(0);
    }),
  );
});
