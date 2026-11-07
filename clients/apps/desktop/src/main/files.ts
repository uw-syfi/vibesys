/** Reading and writing files, as roles the main process's readers and writers take so that tests use memory. */
import {randomUUID} from 'node:crypto';
import {mkdir, readdir, readFile, rename, rm, stat, writeFile} from 'node:fs/promises';
import {basename, dirname, join} from 'node:path';
import * as Clock from 'effect/Clock';
import * as Duration from 'effect/Duration';
import * as Effect from 'effect/Effect';

/** Reads a file's bytes; null when it does not exist. */
export type ReadFile = (path: string) => Effect.Effect<Uint8Array | null, Error>;

const isCode = (error: unknown, code: string): boolean =>
  error instanceof Error && 'code' in error && error.code === code;

const failure = (cause: unknown): Error =>
  cause instanceof Error ? cause : new Error(String(cause));

export const nodeReadFile: ReadFile = path =>
  Effect.tryPromise({
    try: async () => {
      try {
        return await readFile(path);
      } catch (error) {
        if (isCode(error, 'ENOENT')) return null;
        throw error;
      }
    },
    catch: failure,
  });

/** Replaces a file's bytes, creating its directory. A reader sees the old bytes or the new, never half. */
export type WriteFile = (path: string, bytes: Uint8Array) => Effect.Effect<void, Error>;

/** The filesystem steps an atomic replace and a file lock take; `nodeFileOps` is the real one. */
export interface FileOps {
  readonly mkdir: (directory: string) => Promise<void>;
  /** Creates the file and fails when it exists. */
  readonly writeFile: (path: string, bytes: Uint8Array) => Promise<void>;
  /** Creates an empty file, and says whether it did: false when the file already exists. */
  readonly createExclusive: (path: string) => Promise<boolean>;
  readonly rename: (from: string, to: string) => Promise<void>;
  readonly remove: (path: string) => Promise<void>;
  /** When the file was last changed, in milliseconds since the epoch; null when it does not exist. */
  readonly modifiedMs: (path: string) => Promise<number | null>;
  /** The names of the files in a directory; empty when it does not exist. */
  readonly list: (directory: string) => Promise<readonly string[]>;
}

const nodeFileOps: FileOps = {
  mkdir: async directory => {
    await mkdir(directory, {recursive: true});
  },
  writeFile: (path, bytes) => writeFile(path, bytes, {flag: 'wx'}),
  createExclusive: async path => {
    try {
      await writeFile(path, '', {flag: 'wx'});
      return true;
    } catch (error) {
      if (isCode(error, 'EEXIST')) return false;
      throw error;
    }
  },
  rename,
  remove: path => rm(path, {force: true}),
  modifiedMs: async path => {
    try {
      return (await stat(path)).mtimeMs;
    } catch (error) {
      if (isCode(error, 'ENOENT')) return null;
      throw error;
    }
  },
  list: async directory => {
    try {
      return await readdir(directory);
    } catch (error) {
      if (isCode(error, 'ENOENT')) return [];
      throw error;
    }
  },
};

const STAGED_SUFFIX = '.new';

/**
 * A `WriteFile` that stages the bytes in a file of its own, named by `token()`, and renames it over
 * `path`. Each call stages apart, so two writers never share a staging file and a reader never sees
 * half a file. It does not order two writers of one path: the later rename wins whole, so a writer
 * that must keep the other's change reads and writes under a `FileLock`. A failed write removes its
 * staging file; one that never got to (the process died) leaves it, and `sweepStaged` removes it on
 * a later start.
 */
export const makeWriteFile =
  (ops: FileOps, token: () => string): WriteFile =>
  (path, bytes) =>
    Effect.tryPromise({
      try: async () => {
        await ops.mkdir(dirname(path));
        const staged = `${path}.${token()}${STAGED_SUFFIX}`;
        try {
          await ops.writeFile(staged, bytes);
          await ops.rename(staged, path);
        } catch (error) {
          await ops.remove(staged);
          throw error;
        }
      },
      catch: failure,
    });

export const nodeWriteFile: WriteFile = makeWriteFile(nodeFileOps, randomUUID);

/** A staging file this old belongs to a process that is gone: a write takes milliseconds. */
export const STAGED_STALE_MS = 60_000;

/**
 * Removes the staging files `makeWriteFile` left beside `path` (`<name>.<token>.new`) that no write
 * can still be using, and returns their names. A fresh one may be another instance's write in
 * flight and stays.
 */
export const sweepStaged = (ops: FileOps, path: string): Effect.Effect<readonly string[], Error> =>
  Effect.gen(function* () {
    const now = yield* Clock.currentTimeMillis;
    const attempt = <A>(step: () => Promise<A>) => Effect.tryPromise({try: step, catch: failure});
    const prefix = `${basename(path)}.`;
    const swept: string[] = [];
    for (const name of yield* attempt(() => ops.list(dirname(path)))) {
      if (!name.startsWith(prefix) || !name.endsWith(STAGED_SUFFIX)) continue;
      const staged = join(dirname(path), name);
      const modified = yield* attempt(() => ops.modifiedMs(staged));
      if (modified === null || now - modified < STAGED_STALE_MS) continue;
      yield* attempt(() => ops.remove(staged));
      swept.push(name);
    }
    return swept;
  });

export const nodeSweepStaged = (path: string) => sweepStaged(nodeFileOps, path);

/** Runs an effect while holding the lock of a file, across processes. */
export type FileLock = <A, E>(
  path: string,
  effect: Effect.Effect<A, E>,
) => Effect.Effect<A, E | Error>;

/** A lock nobody released for this long belongs to a process that is gone: one is held for milliseconds. */
export const LOCK_STALE_MS = 10_000;
const LOCK_PAUSE_MS = 25;
/** How many times to look before giving up: a little longer than a lock takes to go stale. */
const LOCK_TRIES = LOCK_STALE_MS / LOCK_PAUSE_MS + 40;

/**
 * A `FileLock` made of a file created exclusively beside `path` (`<path>.lock`): whoever creates it
 * holds the lock and removes it when done, and everyone else looks again after a short pause. A lock
 * older than `LOCK_STALE_MS` is a dead holder's and is broken (two waiters breaking the same one at
 * once can both enter, which takes a holder that died and two instances saving in the same
 * millisecond). Giving up, after about as long as a lock takes to go stale, fails with the file named
 * instead of waiting for ever.
 */
export const makeFileLock =
  (ops: FileOps): FileLock =>
  (path, effect) => {
    const lock = `${path}.lock`;
    const attempt = <A>(step: () => Promise<A>) => Effect.tryPromise({try: step, catch: failure});
    /** Whether the lock was taken now; when it was not, whether its holder is gone (so it is broken). */
    const take = Effect.gen(function* () {
      if (yield* attempt(() => ops.createExclusive(lock))) return 'taken' as const;
      const modified = yield* attempt(() => ops.modifiedMs(lock));
      const now = yield* Clock.currentTimeMillis;
      if (modified === null || now - modified <= LOCK_STALE_MS) return 'held' as const;
      yield* attempt(() => ops.remove(lock));
      return 'broken' as const;
    });
    const acquire = Effect.gen(function* () {
      yield* attempt(() => ops.mkdir(dirname(path)));
      for (let tried = 0; tried <= LOCK_TRIES; tried++) {
        const outcome = yield* take;
        if (outcome === 'taken') return;
        if (outcome === 'held') yield* Effect.sleep(Duration.millis(LOCK_PAUSE_MS));
      }
      return yield* Effect.fail(new Error(`${path} is being saved by another instance`));
    });
    return Effect.acquireUseRelease(
      acquire,
      () => effect,
      () => attempt(() => ops.remove(lock)).pipe(Effect.ignore),
    );
  };

export const nodeFileLock: FileLock = makeFileLock(nodeFileOps);
