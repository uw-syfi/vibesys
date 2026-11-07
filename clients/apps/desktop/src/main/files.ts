/** Reading and writing files, as roles the main process's readers and writers take so that tests use memory. */
import {randomUUID} from 'node:crypto';
import {mkdir, readFile, rename, rm, writeFile} from 'node:fs/promises';
import {dirname} from 'node:path';
import * as Effect from 'effect/Effect';

/** Reads a file's bytes; null when it does not exist. */
export type ReadFile = (path: string) => Effect.Effect<Uint8Array | null, Error>;

export const nodeReadFile: ReadFile = path =>
  Effect.tryPromise({
    try: async () => {
      try {
        return await readFile(path);
      } catch (error) {
        if (error instanceof Error && 'code' in error && error.code === 'ENOENT') return null;
        throw error;
      }
    },
    catch: cause => (cause instanceof Error ? cause : new Error(String(cause))),
  });

/** Replaces a file's bytes, creating its directory. A reader sees the old bytes or the new, never half. */
export type WriteFile = (path: string, bytes: Uint8Array) => Effect.Effect<void, Error>;

/** The filesystem steps an atomic replace takes; `nodeFileOps` is the real one. */
export interface FileOps {
  readonly mkdir: (directory: string) => Promise<void>;
  readonly writeFile: (path: string, bytes: Uint8Array) => Promise<void>;
  readonly rename: (from: string, to: string) => Promise<void>;
  readonly remove: (path: string) => Promise<void>;
}

export const nodeFileOps: FileOps = {
  mkdir: async directory => {
    await mkdir(directory, {recursive: true});
  },
  writeFile: (path, bytes) => writeFile(path, bytes, {flag: 'wx'}),
  rename,
  remove: path => rm(path, {force: true}),
};

/**
 * A `WriteFile` that stages the bytes in a file of its own, named by `token()`, and renames it over
 * `path`. Each call stages apart, so two writers of one path (two windows, two processes) never
 * share a staging file: the later rename wins whole. A failed write removes its staging file.
 */
export const makeWriteFile =
  (ops: FileOps, token: () => string): WriteFile =>
  (path, bytes) =>
    Effect.tryPromise({
      try: async () => {
        await ops.mkdir(dirname(path));
        const staged = `${path}.${token()}.new`;
        try {
          await ops.writeFile(staged, bytes);
          await ops.rename(staged, path);
        } catch (error) {
          await ops.remove(staged);
          throw error;
        }
      },
      catch: cause => (cause instanceof Error ? cause : new Error(String(cause))),
    });

export const nodeWriteFile: WriteFile = makeWriteFile(nodeFileOps, randomUUID);
