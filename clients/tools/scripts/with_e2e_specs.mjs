#!/usr/bin/env node

/**
 * Runs an app's end-to-end command only when this job's plan holds one of the app's specs.
 *
 * A job runs a slice of the specs of three apps (`VIBESYS_E2E_SHARD`, and `VIBESYS_E2E_SPECS` for
 * a pull request), and an app's command builds the app before it runs a spec. A job whose slice
 * holds none of the app's specs would still pay that build, about a minute for the Electron app,
 * to run nothing. The plan is the one the app's Playwright config reads (`e2eTestMatch`), so the
 * two cannot disagree.
 *
 * Usage: `node with_e2e_specs.mjs <app> <command>`, the command run by the shell.
 */

import {spawnSync} from 'node:child_process';
import {resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {e2eTestMatch} from '../e2e/shard.mts';

/**
 * @param {string[]} argv `<app> <command>`
 * @param {Readonly<Record<string, string | undefined>>} [env]
 * @returns {number} the exit status
 */
export function run(argv, env = process.env) {
  const [app, command, ...rest] = argv;
  if (app === undefined || command === undefined || rest.length > 0) {
    console.error('usage: with_e2e_specs.mjs <app> <command>');
    return 2;
  }
  if (e2eTestMatch(app, env).length === 0) {
    console.log(`e2e: no ${app} spec in this job's plan; nothing to build or run`);
    return 0;
  }
  const child = spawnSync(command, {shell: true, stdio: 'inherit', env});
  return child.status ?? 1;
}

if (process.argv[1] !== undefined && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = run(process.argv.slice(2));
}
