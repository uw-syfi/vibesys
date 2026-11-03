#!/usr/bin/env node

/**
 * Run one package's Vitest tests (`@effect/vitest`) with the shared hang guard.
 *
 * Only the Effect packages use Vitest, because `@effect/vitest` provides `it.effect` and the
 * virtual `TestClock`; every other package stays on `run_bun_tests.mjs`. The runner takes test
 * roots only, and the flags live in `test_runner_config.mjs` so packages cannot drift apart or
 * switch the guard off (`check_test_runners.mjs` rejects a `vitest` call that bypasses it).
 * Vitest itself fails a run that finds no test file or cannot import one, so there is no
 * collection gate to repeat here.
 */

import {spawn} from 'node:child_process';
import {createRequire} from 'node:module';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {VITEST_ARGUMENTS} from './test_runner_config.mjs';

/** The roots as path filters Vitest matches against test file paths, or a rejection. */
export function parseRoots(argv) {
  if (argv.length === 0) {
    throw new Error('usage: node ../../tools/scripts/run_vitest.mjs <test root>...');
  }
  for (const argument of argv) {
    if (argument.startsWith('-')) {
      throw new Error(
        `${argument}: this runner takes test roots only; the hang guard is shared. ` +
          'Run `vitest` directly for ad-hoc flags',
      );
    }
  }
  return argv.map(root => root.replace(/^\.\//, '').replace(/\/+$/, ''));
}

/** The entry script of the Vitest installed for the package in `directory`. */
function vitestEntry(directory) {
  const manifest = createRequire(join(directory, 'package.json')).resolve('vitest/package.json');
  return join(dirname(manifest), 'vitest.mjs');
}

function main(argv) {
  const roots = parseRoots(argv);
  const child = spawn(
    process.execPath,
    [vitestEntry(process.cwd()), ...VITEST_ARGUMENTS, ...roots],
    {
      stdio: 'inherit',
    },
  );
  child.on('error', error => {
    console.error(error.message);
    process.exitCode = 1;
  });
  child.on('close', (status, signal) => {
    if (signal !== null) console.error(`vitest was killed by ${signal}`);
    process.exitCode = signal === null ? (status ?? 1) : 1;
  });
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    main(process.argv.slice(2));
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
