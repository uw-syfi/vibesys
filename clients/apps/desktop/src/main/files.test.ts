import {describe, expect, it} from '@effect/vitest';
import * as Effect from 'effect/Effect';
import * as Fiber from 'effect/Fiber';
import * as TestClock from 'effect/testing/TestClock';
import fc from 'fast-check';
import {directory} from '../testing/main/memory-directory.js';
import {LOCK_STALE_MS, makeFileLock, makeWriteFile, STAGED_STALE_MS, sweepStaged} from './files.js';

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
          expect(contents).toContainEqual(files.get('/data/preferences.pb')?.bytes);
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

describe('the sweep of staging files', () => {
  it.effect('removes the old ones a dead process left, and nothing else', () =>
    Effect.gen(function* () {
      const {files, ops, put} = directory({now: 0});
      yield* TestClock.setTime(10 * STAGED_STALE_MS);
      put('/data/preferences.pb', Uint8Array.of(1));
      put('/data/preferences.pb.1111.new', Uint8Array.of(2));
      put('/data/preferences.pb.2222.new', Uint8Array.of(3));
      put('/data/hosts.pb.3333.new', Uint8Array.of(4));
      put('/data/preferences.pb.lock', new Uint8Array());
      // One more is fresh: another instance's write may be in flight.
      files.set('/data/preferences.pb.9999.new', {
        bytes: Uint8Array.of(5),
        modifiedMs: 10 * STAGED_STALE_MS - 1000,
      });
      for (const name of ['1111', '2222']) {
        const file = files.get(`/data/preferences.pb.${name}.new`);
        if (file !== undefined) file.modifiedMs = 0;
      }
      const swept = yield* sweepStaged(ops, '/data/preferences.pb');
      expect([...swept].sort()).toEqual(['preferences.pb.1111.new', 'preferences.pb.2222.new']);
      expect([...files.keys()].sort()).toEqual([
        '/data/hosts.pb.3333.new',
        '/data/preferences.pb',
        '/data/preferences.pb.9999.new',
        '/data/preferences.pb.lock',
      ]);
    }),
  );
});

describe('a file lock', () => {
  it.effect('lets one holder in at a time, and everyone in turn', () =>
    Effect.gen(function* () {
      const {files, ops} = directory();
      const lock = makeFileLock(ops);
      let inside = 0;
      let widest = 0;
      let entered = 0;
      const section = Effect.gen(function* () {
        inside += 1;
        entered += 1;
        widest = Math.max(widest, inside);
        yield* Effect.yieldNow;
        inside -= 1;
      });
      const all = yield* Effect.all(
        [1, 2, 3, 4].map(() => lock('/data/preferences.pb', section)),
        {concurrency: 'unbounded'},
      ).pipe(Effect.forkChild);
      for (let step = 0; step < 40; step++) yield* TestClock.adjust('25 millis');
      yield* Fiber.join(all);
      expect(entered).toBe(4);
      expect(widest).toBe(1);
      expect([...files.keys()]).toEqual([]);
    }),
  );

  it.effect('lets go when its effect fails', () =>
    Effect.gen(function* () {
      const {files, ops} = directory();
      const lock = makeFileLock(ops);
      const error = yield* Effect.flip(lock('/data/preferences.pb', Effect.fail(new Error('no'))));
      expect(error.message).toBe('no');
      expect(files.size).toBe(0);
    }),
  );

  it.effect('breaks the lock of a holder that is gone', () =>
    Effect.gen(function* () {
      const {files, ops, put} = directory();
      const lock = makeFileLock(ops);
      yield* TestClock.setTime(10 * LOCK_STALE_MS);
      put('/data/preferences.pb.lock', new Uint8Array());
      expect(yield* lock('/data/preferences.pb', Effect.succeed('in'))).toBe('in');
      expect(files.has('/data/preferences.pb.lock')).toBe(false);
    }),
  );

  it.effect('gives up on a holder that is still there, naming the file, and leaves its lock', () =>
    Effect.gen(function* () {
      const {files, ops, put} = directory();
      // A lock that is always fresh: its holder keeps going.
      const lock = makeFileLock({
        ...ops,
        modifiedMs: () => Promise.resolve(Number.MAX_SAFE_INTEGER),
      });
      put('/data/preferences.pb.lock', new Uint8Array());
      const waiting = yield* lock('/data/preferences.pb', Effect.succeed('in')).pipe(
        Effect.flip,
        Effect.forkChild,
      );
      for (let step = 0; step < 800; step++) yield* TestClock.adjust('25 millis');
      const refused = yield* Fiber.join(waiting);
      expect(refused.message).toContain('/data/preferences.pb');
      expect(files.has('/data/preferences.pb.lock')).toBe(true);
    }),
  );
});
