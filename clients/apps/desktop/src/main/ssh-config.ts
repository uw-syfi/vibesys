/**
 * The host aliases of the person's ssh config, read from its files: the main config and every file
 * it `Include`s, a glob in the last path segment expanded as ssh does (in lexical order), a
 * relative path read from beside the config, `~` from the home directory. A file that is missing
 * or cannot be read has no aliases and costs nothing else; includes nest to a bounded depth and a
 * file is read once, so a loop ends. The config's path is `~/.ssh/config`, or the one the app was
 * told to use (`VIBESYS_DESKTOP_SSH_CONFIG`, for tests and captures); `sshConfigArgs` gives ssh the
 * same file then, so what is offered is what ssh will use.
 */
import {dirname, isAbsolute, join} from 'node:path';
import * as Effect from 'effect/Effect';
import {parseSshConfig} from '../state/settings/ssh-config.js';
import type {ListDirectory, ReadFile} from './files.js';

/** The environment variable that points the app at another ssh config. */
export const SSH_CONFIG_ENV = 'VIBESYS_DESKTOP_SSH_CONFIG';
const MAX_DEPTH = 8;

export interface SshConfigSource {
  /** Every alias, in the order the files declare them, each once. */
  readonly aliases: Effect.Effect<readonly string[]>;
}

/** `*` and `?` in one path segment as a regular expression. */
const globOf = (segment: string): RegExp =>
  new RegExp(
    `^${segment
      .replace(/[.+^${}()|[\]\\]/g, '\\$&')
      .replace(/\*/g, '.*')
      .replace(/\?/g, '.')}$`,
  );

export function makeSshConfigSource(
  files: {readonly read: ReadFile; readonly list: ListDirectory},
  location: {readonly config: string; readonly home: string},
  log: (line: string) => void,
): SshConfigSource {
  const base = dirname(location.config);
  /** The files an Include names, in order. */
  const expand = (pattern: string): Effect.Effect<readonly string[]> =>
    Effect.gen(function* () {
      const path = pattern.startsWith('~/')
        ? join(location.home, pattern.slice(2))
        : isAbsolute(pattern)
          ? pattern
          : join(base, pattern);
      const directory = dirname(path);
      const leaf = path.slice(directory.length + 1);
      if (/[*?]/.test(directory)) {
        log(`ssh config: a wildcard in a directory is not followed: ${pattern}`);
        return [];
      }
      if (!/[*?]/.test(leaf)) return [path];
      const names = yield* files.list(directory);
      const glob = globOf(leaf);
      return names
        .filter(name => glob.test(name))
        .sort()
        .map(name => join(directory, name));
    }).pipe(
      Effect.catch(error =>
        Effect.sync(() => {
          log(`ssh config: could not list ${pattern}: ${error.message}`);
          return [] as readonly string[];
        }),
      ),
    );
  const collect = (
    path: string,
    depth: number,
    seen: Set<string>,
    out: string[],
  ): Effect.Effect<void> =>
    Effect.gen(function* () {
      if (depth > MAX_DEPTH || seen.has(path)) return;
      seen.add(path);
      const bytes = yield* files.read(path).pipe(
        Effect.catch(error =>
          Effect.sync(() => {
            log(`ssh config: could not read ${path}: ${error.message}`);
            return null;
          }),
        ),
      );
      if (bytes === null) return;
      const entries = parseSshConfig(new TextDecoder().decode(bytes));
      // Host lines before an Include and after it keep the order ssh reads them in only roughly;
      // an alias is offered wherever it is declared, so the order is by file, then by include.
      out.push(...entries.aliases);
      for (const include of entries.includes) {
        for (const next of yield* expand(include)) yield* collect(next, depth + 1, seen, out);
      }
    });
  return {
    aliases: Effect.gen(function* () {
      const out: string[] = [];
      yield* collect(location.config, 0, new Set(), out);
      return [...new Set(out)];
    }),
  };
}

/** `-F <config>` when the app was told which config to use; none for the person's own. */
export const sshConfigArgs = (explicit: string | undefined): readonly string[] =>
  explicit === undefined || explicit === '' ? [] : ['-F', explicit];
