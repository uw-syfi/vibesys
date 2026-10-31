import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {chmodSync, existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {delimiter, dirname, join} from 'node:path';
import {test} from 'node:test';
import {fileURLToPath} from 'node:url';
import {DIRECTORY_VARIABLE} from './with_derived_replays.mjs';

const SCRIPT = join(dirname(fileURLToPath(import.meta.url)), 'with_derived_replays.mjs');
const READER = join(dirname(SCRIPT), '../../libs/run-fixtures/src/derived.ts');
const SHOW = ['-e', `process.stdout.write(process.env.${DIRECTORY_VARIABLE} ?? 'unset')`];

/**
 * A `uv` that stands for the two Python commands the wrapper runs: `derived_replays path`
 * (exit `found`, printing `directory`) and `materialize_replays` (exit `made`, printing progress
 * on stderr and then `generated`). Each call is appended to `log`.
 */
function fakeUv(directory, {found, made}) {
  const log = join(directory, 'calls.log');
  const program = join(directory, 'uv');
  writeFileSync(
    program,
    `#!/bin/sh
echo "$7 $8" >> '${log}'
case "$7" in
  tests.support.derived_replays)
    [ ${found} -eq 0 ] && echo /cache/found
    exit ${found} ;;
  tests.support.materialize_replays)
    echo 'derived replays: shared run' >&2
    [ ${made} -eq 0 ] && echo /cache/generated
    exit ${made} ;;
esac
exit 99
`,
  );
  chmodSync(program, 0o755);
  return log;
}

function wrapped(behaviour, command) {
  const directory = mkdtempSync(join(tmpdir(), 'with-derived-replays-'));
  try {
    const log = fakeUv(directory, behaviour);
    const result = spawnSync(process.execPath, [SCRIPT, ...command], {
      encoding: 'utf8',
      env: {...process.env, PATH: `${directory}${delimiter}${process.env.PATH}`},
    });
    const calls = existsSync(log) ? readFileSync(log, 'utf8').trim().split('\n') : [];
    return {...result, calls, directory};
  } finally {
    rmSync(directory, {recursive: true, force: true});
  }
}

test('a set that exists is used without generating anything', () => {
  const run = wrapped({found: 0, made: 0}, [process.execPath, ...SHOW]);
  assert.equal(run.status, 0);
  assert.equal(run.stdout, '/cache/found');
  assert.deepEqual(run.calls, ['tests.support.derived_replays path']);
});

test('a missing set is generated first and the command reads the generated directory', () => {
  const run = wrapped({found: 1, made: 0}, [process.execPath, ...SHOW]);
  assert.equal(run.status, 0);
  assert.equal(run.stdout, '/cache/generated');
  assert.deepEqual(run.calls, [
    'tests.support.derived_replays path',
    'tests.support.materialize_replays',
  ]);
  assert.match(run.stderr, /derived replays: shared run/);
});

test('a generator that fails stops the run before the command, naming the failure', () => {
  const run = wrapped({found: 1, made: 3}, [process.execPath, ...SHOW]);
  assert.notEqual(run.status, 0);
  assert.equal(run.stdout, '');
  assert.match(run.stderr, /generating the derived replays failed \(exit 3\)/);
});

test('the status of the command is the status of the run', () => {
  for (const status of [0, 1, 7]) {
    const run = wrapped({found: 0, made: 0}, [process.execPath, '-e', `process.exit(${status})`]);
    assert.equal(run.status, status);
  }
});

test('a command ended by a signal fails the run and names the signal', () => {
  const run = wrapped({found: 0, made: 0}, [
    process.execPath,
    '-e',
    "process.kill(process.pid, 'SIGTERM')",
  ]);
  assert.equal(run.status, 1);
  assert.match(run.stderr, /ended by SIGTERM/);
});

test('no command is a usage error', () => {
  const run = wrapped({found: 0, made: 0}, []);
  assert.equal(run.status, 2);
  assert.match(run.stderr, /usage: with_derived_replays\.mjs COMMAND/);
});

test('the variable the wrapper sets is the one the reader reads', () => {
  const declared = /DERIVED_REPLAYS_VARIABLE = '([A-Z_]+)'/.exec(readFileSync(READER, 'utf8'));
  assert.equal(declared?.[1], DIRECTORY_VARIABLE);
});
