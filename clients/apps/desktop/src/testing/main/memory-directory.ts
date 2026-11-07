/** A directory in memory behind the main process's `FileOps`, for tests of its writers and locks. */
import type {FileOps} from '../../main/files.js';

/**
 * A directory in memory: every file with when it was last changed. Every step yields, so concurrent
 * writers interleave; `clock` is what the filesystem's own time says.
 */
export const directory = (clock: {now: number} = {now: 0}) => {
  const files = new Map<string, {bytes: Uint8Array; modifiedMs: number}>();
  const staged: string[] = [];
  const put = (path: string, bytes: Uint8Array) => files.set(path, {bytes, modifiedMs: clock.now});
  const ops: FileOps = {
    mkdir: () => Promise.resolve(),
    writeFile: async (path, bytes) => {
      // The real write is exclusive: it fails when the staging file already exists.
      if (files.has(path)) throw new Error(`EEXIST: ${path}`);
      staged.push(path);
      put(path, bytes);
      await Promise.resolve();
    },
    createExclusive: async path => {
      await Promise.resolve();
      if (files.has(path)) return false;
      put(path, new Uint8Array());
      return true;
    },
    rename: async (from, to) => {
      await Promise.resolve();
      const file = files.get(from);
      if (file === undefined) throw new Error(`ENOENT: ${from}`);
      files.delete(from);
      files.set(to, file);
    },
    remove: path => {
      files.delete(path);
      return Promise.resolve();
    },
    modifiedMs: path => Promise.resolve(files.get(path)?.modifiedMs ?? null),
    list: dir =>
      Promise.resolve(
        [...files.keys()]
          .filter(path => path.startsWith(`${dir}/`))
          .map(path => path.slice(dir.length + 1)),
      ),
  };
  return {files, staged, ops, put};
};
