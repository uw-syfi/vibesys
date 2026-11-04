/**
 * The private directory for ssh master sockets: short (Unix socket paths are capped near 104
 * bytes, and `%C` adds 40), owned by this user, and closed to everyone else. A directory that
 * already exists is trusted only when it is a real directory owned by `uid` with no group or
 * other access, so no one else can have planted or opened the sockets in it.
 */
import {lstat, mkdir} from 'node:fs/promises';
import * as Effect from 'effect/Effect';
import {GatewayFailed} from './errors.js';

/** What the check needs of a directory entry. */
export interface DirectoryEntry {
  readonly kind: 'directory' | 'other';
  readonly uid: number;
  readonly mode: number;
}

export interface ControlFilesystem {
  /** Create `path` with mode 0700; `false` when it already exists. */
  readonly createPrivate: (path: string) => Effect.Effect<boolean, GatewayFailed>;
  /** The entry at `path` itself (a symlink is `other`). */
  readonly inspect: (path: string) => Effect.Effect<DirectoryEntry, GatewayFailed>;
}

const failure = (what: string, cause: unknown): GatewayFailed =>
  new GatewayFailed({
    message: `${what}: ${cause instanceof Error ? cause.message : String(cause)}`,
  });

const nodeControlFilesystem: ControlFilesystem = {
  createPrivate: path =>
    Effect.tryPromise({
      try: async () => {
        try {
          await mkdir(path, {mode: 0o700});
          return true;
        } catch (error) {
          if (error instanceof Error && 'code' in error && error.code === 'EEXIST') return false;
          throw error;
        }
      },
      catch: cause => failure(`could not create ${path}`, cause),
    }),
  inspect: path =>
    Effect.tryPromise({
      try: async () => {
        const entry = await lstat(path);
        return {
          kind: entry.isDirectory() ? 'directory' : 'other',
          uid: entry.uid,
          mode: entry.mode,
        } as const;
      },
      catch: cause => failure(`could not inspect ${path}`, cause),
    }),
};

/** The control directory for `uid`, created if needed and checked. */
export const controlDirectory = (
  uid: number,
  filesystem: ControlFilesystem = nodeControlFilesystem,
): Effect.Effect<string, GatewayFailed> =>
  Effect.gen(function* () {
    const directory = `/tmp/vsd-${uid}`;
    yield* filesystem.createPrivate(directory);
    const entry = yield* filesystem.inspect(directory);
    if (entry.kind !== 'directory' || entry.uid !== uid || (entry.mode & 0o077) !== 0)
      return yield* new GatewayFailed({
        message: `${directory} must be a real directory owned by you and private; remove it and try again`,
      });
    return directory;
  });
