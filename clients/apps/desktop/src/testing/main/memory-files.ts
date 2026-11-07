/** Files held in memory, for the main process's readers and writers: what each wrote, by path. */
import * as Effect from 'effect/Effect';
import type {FileLock, ReadFile, WriteFile} from '../../main/files.js';

export interface MemoryFiles {
  readonly read: ReadFile;
  readonly write: WriteFile;
  /** One process: nothing else holds the file, so the lock runs its effect at once. */
  readonly lock: FileLock;
  /** The bytes at `path` now, or undefined. */
  readonly at: (path: string) => Uint8Array | undefined;
  /** Every path written, in order. */
  readonly written: () => readonly string[];
}

export function makeMemoryFiles(initial: Readonly<Record<string, Uint8Array>> = {}): MemoryFiles {
  const files = new Map(Object.entries(initial));
  const written: string[] = [];
  return {
    read: path => Effect.sync(() => files.get(path) ?? null),
    write: (path, bytes) =>
      Effect.sync(() => {
        files.set(path, bytes);
        written.push(path);
      }),
    lock: (_path, effect) => effect,
    at: path => files.get(path),
    written: () => written,
  };
}
