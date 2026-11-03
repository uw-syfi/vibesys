/** Reading files, as a role the main process's readers take so that tests read from memory. */
import {readFile} from 'node:fs/promises';
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
