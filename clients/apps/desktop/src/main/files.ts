/** Reading and writing files, as roles the main process's readers and writers take so that tests use memory. */
import {mkdir, readFile, rename, writeFile} from 'node:fs/promises';
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

export const nodeWriteFile: WriteFile = (path, bytes) =>
  Effect.tryPromise({
    try: async () => {
      await mkdir(dirname(path), {recursive: true});
      const staged = `${path}.new`;
      await writeFile(staged, bytes);
      await rename(staged, path);
    },
    catch: cause => (cause instanceof Error ? cause : new Error(String(cause))),
  });
