#!/usr/bin/env node

/**
 * Run a command with the native replays it reads made first: `with_derived_replays.mjs CMD ARGS...`.
 *
 * The replays are not committed. Python generates them from the declarations under the hash of
 * everything they derive from (`tests/support/derived_replays.py`), so a change to the projector,
 * a schema or a declaration names a new set and a stale one is never read. This script asks Python
 * for the set the current inputs name; only when it is missing does it run the generator, which
 * loads the product and takes about two minutes for the two 600 idea scenarios. It exports the
 * set's directory as `VIBESYS_DERIVED_REPLAYS`, which `@vibesys/run-fixtures/derived` reads, and
 * runs the command with that environment. The generation runs here, before any test, and never in
 * one: a test has a 30 s bound that a projection of 600 ideas would exceed.
 *
 * The exit status is the command's; a command ended by a signal exits 1 naming it.
 */

import {spawn, spawnSync} from 'node:child_process';
import {dirname, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

export const DIRECTORY_VARIABLE = 'VIBESYS_DERIVED_REPLAYS';
const REPOSITORY = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');

/** `uv run python -m MODULE ARGS` at the repository root; stdout is returned, stderr is shown. */
function python(module, ...args) {
  return spawnSync(
    'uv',
    ['run', '--locked', '--project', REPOSITORY, 'python', '-m', module, ...args],
    {
      cwd: REPOSITORY,
      encoding: 'utf8',
      stdio: ['ignore', 'pipe', 'inherit'],
    },
  );
}

/** The directory of the set the current inputs name, generated first when it is missing. */
export function derivedReplaysDirectory() {
  const found = python('tests.support.derived_replays', 'path');
  if (found.status === 0) return found.stdout.trim();
  const made = python('tests.support.materialize_replays');
  if (made.status !== 0) {
    throw new Error(`generating the derived replays failed (exit ${made.status}); see above`);
  }
  return made.stdout.trim().split('\n').at(-1);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const [command, ...args] = process.argv.slice(2);
  if (command === undefined) {
    console.error('usage: with_derived_replays.mjs COMMAND [ARGS...]');
    process.exit(2);
  }
  const child = spawn(command, args, {
    stdio: 'inherit',
    env: {...process.env, [DIRECTORY_VARIABLE]: derivedReplaysDirectory()},
  });
  child.on('exit', (code, signal) => {
    if (signal !== null) console.error(`${command} ended by ${signal}`);
    process.exit(code ?? 1);
  });
}
