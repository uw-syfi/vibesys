#!/usr/bin/env node

/**
 * Run a command with the native replays it reads made first: `with_derived_replays.mjs CMD ARGS...`.
 *
 * The replays are not committed. Python generates them from the declarations under a hash of
 * everything they can derive from (`tests/support/derived_replays.py`), so a change to the
 * projector, a library, a lock file or a declaration names a new set and a stale one is never read.
 * This script asks Python for the set the current inputs name; only when it is missing does it run
 * the generator, which loads the product and takes about fifteen seconds. It exports the set's
 * directory as `VIBESYS_DERIVED_REPLAYS`, which `@vibesys/run-fixtures/derived` reads, and runs the
 * command with that environment. The generation runs here, before any test, and never in one.
 *
 * This is a layer between the package manager and the tool, so it owns the tool's lifetime: SIGINT,
 * SIGTERM and SIGHUP reach whatever it is running (the generator, then the command), and the run
 * ends with the command's exit status, or 128 plus the signal's number when a signal ended it. A
 * Ctrl-C at a terminal reaches the command twice (the terminal signals the whole group and this
 * script forwards it); a tool that treats a second SIGINT as "force quit" quits at once.
 */

import {spawn} from 'node:child_process';
import {constants} from 'node:os';
import {dirname, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

export const DIRECTORY_VARIABLE = 'VIBESYS_DERIVED_REPLAYS';
const FORWARDED = ['SIGINT', 'SIGTERM', 'SIGHUP'];
const REPOSITORY = resolve(dirname(fileURLToPath(import.meta.url)), '../../..');

/** Thrown when a signal ended a stage before the command could start. */
class Interrupted extends Error {
  constructor(signal) {
    super(`interrupted by ${signal}`);
    this.signal = signal;
  }
}

/**
 * Run `command`, forwarding the signals this process receives while it runs. `capture` returns its
 * stdout; its stderr always reaches ours. Resolves with how it ended.
 */
function run(command, args, {capture = false, cwd, env} = {}) {
  return new Promise((done, fail) => {
    const child = spawn(command, args, {
      cwd,
      env,
      stdio: capture ? ['ignore', 'pipe', 'inherit'] : 'inherit',
    });
    let stdout = '';
    child.stdout?.on('data', chunk => {
      stdout += chunk;
    });
    const forwards = FORWARDED.map(signal => [signal, () => child.kill(signal)]);
    for (const [signal, forward] of forwards) process.on(signal, forward);
    const release = () => {
      for (const [signal, forward] of forwards) process.off(signal, forward);
    };
    child.on('error', error => {
      release();
      fail(error);
    });
    child.on('close', (status, signal) => {
      release();
      done({status, signal, stdout});
    });
  });
}

/** `uv run python -m MODULE ARGS` at the repository root. */
function python(module, ...args) {
  const command = ['run', '--locked', '--project', REPOSITORY, 'python', '-m', module, ...args];
  return run('uv', command, {capture: true, cwd: REPOSITORY});
}

function ended(stage, result) {
  if (result.signal !== null) throw new Interrupted(result.signal);
  return result.status === 0 ? undefined : `${stage} failed (exit ${result.status}); see above`;
}

/** The directory of the set the current inputs name, generated first when it is missing. */
export async function derivedReplaysDirectory() {
  const found = await python('tests.support.derived_replays', 'path');
  if (ended('looking for the derived replays', found) === undefined) return found.stdout.trim();
  const made = await python('tests.support.materialize_replays');
  const failure = ended('generating the derived replays', made);
  if (failure !== undefined) throw new Error(failure);
  return made.stdout.trim().split('\n').at(-1);
}

/** Run the command with the set exported; the status the whole run exits with. */
export async function withDerivedReplays([command, ...args]) {
  if (command === undefined) {
    console.error('usage: with_derived_replays.mjs COMMAND [ARGS...]');
    return 2;
  }
  try {
    const directory = await derivedReplaysDirectory();
    const result = await run(command, args, {
      env: {...process.env, [DIRECTORY_VARIABLE]: directory},
    });
    if (result.signal === null) return result.status ?? 1;
    console.error(`${command} ended by ${result.signal}`);
    return 128 + constants.signals[result.signal];
  } catch (error) {
    if (error instanceof Interrupted) {
      console.error(`${error.message}; the command did not start`);
      return 128 + constants.signals[error.signal];
    }
    console.error(error.message);
    return 1;
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = await withDerivedReplays(process.argv.slice(2));
}
