import assert from 'node:assert/strict';
import {spawn, spawnSync} from 'node:child_process';
import {
  chmodSync,
  existsSync,
  mkdtempSync,
  readdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import {tmpdir} from 'node:os';
import {delimiter, dirname, join} from 'node:path';
import {test} from 'node:test';
import {fileURLToPath} from 'node:url';
import {DIRECTORY_VARIABLE} from './with_derived_replays.mjs';
import {workspaceLayout} from './workspace_layout.mjs';

const SCRIPT = join(dirname(fileURLToPath(import.meta.url)), 'with_derived_replays.mjs');
const READER = join(dirname(SCRIPT), '../../libs/run-fixtures/src/derived.ts');
const SHOW = ['-e', `process.stdout.write(process.env.${DIRECTORY_VARIABLE} ?? 'unset')`];

/**
 * A `uv` that stands for the two Python commands the wrapper runs: `derived_replays path`
 * (exit `found`, printing `directory`) and `materialize_replays` (exit `made`, printing progress
 * on stderr and then `generated`). `made: 'block'` generates until a signal ends it. Each call is
 * appended to `log`.
 */
function fakeUv(directory, {found, made}) {
  const log = join(directory, 'calls.log');
  const program = join(directory, 'uv');
  const generate =
    made === 'block'
      ? 'exec sleep 60'
      : `[ ${made} -eq 0 ] && echo /cache/generated\n    exit ${made}`;
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
    ${generate} ;;
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

test('a command ended by a signal ends the run with 128 plus its number, naming it', () => {
  const run = wrapped({found: 0, made: 0}, [
    process.execPath,
    '-e',
    "process.kill(process.pid, 'SIGTERM')",
  ]);
  assert.equal(run.status, 143);
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

/** The text a stream has produced so far, and a promise for when it matches a pattern. */
function watch(stream) {
  let text = '';
  const checks = new Set();
  stream.setEncoding('utf8');
  stream.on('data', chunk => {
    text += chunk;
    for (const check of [...checks]) check();
  });
  return {
    text: () => text,
    matches: pattern =>
      new Promise(resolve => {
        const check = () => {
          if (!pattern.test(text)) return;
          checks.delete(check);
          resolve();
        };
        checks.add(check);
        check();
      }),
  };
}

/** Start the wrapper without waiting for it, with a fake `uv` first on its path. */
function start(behaviour, command) {
  const directory = mkdtempSync(join(tmpdir(), 'with-derived-replays-'));
  fakeUv(directory, behaviour);
  const child = spawn(process.execPath, [SCRIPT, ...command], {
    env: {...process.env, PATH: `${directory}${delimiter}${process.env.PATH}`},
  });
  const stdout = watch(child.stdout);
  const stderr = watch(child.stderr);
  const exited = new Promise(done => child.on('close', (code, signal) => done({code, signal})));
  const cleanup = () => {
    child.kill('SIGKILL');
    rmSync(directory, {recursive: true, force: true});
  };
  return {child, stdout, stderr, exited, cleanup};
}

const WAIT_FOR_SIGNAL = "console.log('ready'); setInterval(() => {}, 1 << 30);";

for (const [signal, status] of [
  ['SIGINT', 9],
  ['SIGTERM', 7],
  ['SIGHUP', 5],
]) {
  test(`${signal} sent to the wrapper reaches the command and the run ends with its status`, async () => {
    const run = start({found: 0, made: 0}, [
      process.execPath,
      '-e',
      `process.on('${signal}', () => process.exit(${status})); ${WAIT_FOR_SIGNAL}`,
    ]);
    try {
      await run.stdout.matches(/ready/);
      run.child.kill(signal);
      assert.deepEqual(await run.exited, {code: status, signal: null});
    } finally {
      run.cleanup();
    }
  });
}

test('a command that does not handle a signal is ended by it and the run exits 128 plus its number', async () => {
  const run = start({found: 0, made: 0}, [process.execPath, '-e', WAIT_FOR_SIGNAL]);
  try {
    await run.stdout.matches(/ready/);
    run.child.kill('SIGTERM');
    assert.deepEqual(await run.exited, {code: 143, signal: null});
    assert.match(run.stderr.text(), /ended by SIGTERM/);
  } finally {
    run.cleanup();
  }
});

test('a signal during generation reaches the generator and the command never starts', async () => {
  const run = start({found: 1, made: 'block'}, [process.execPath, '-e', "console.log('ran')"]);
  try {
    await run.stderr.matches(/derived replays: shared run/);
    run.child.kill('SIGTERM');
    assert.deepEqual(await run.exited, {code: 143, signal: null});
    assert.equal(run.stdout.text(), '');
    assert.match(run.stderr.text(), /interrupted by SIGTERM; the command did not start/);
  } finally {
    run.cleanup();
  }
});

const WORKSPACE = join(dirname(SCRIPT), '../..');
const READER_IMPORT = '@vibesys/run-fixtures/derived';

/**
 * The scripts of a package that start a tool on the replays without going through the wrapper.
 * A script that starts Playwright, the fixture dev app or the Linux e2e run needs it when any file
 * of the package reads a replay; one that starts the bun runner needs it when a file under `src`
 * does.
 */
function unwrapped(manifest, {anywhere, inSource}) {
  return Object.entries(manifest.scripts ?? {})
    .filter(([, script]) => {
      if (script.includes('with_derived_replays.mjs')) return false;
      const startsE2e = /playwright\/test\/cli\.js|dev-fixture|linux-e2e\.sh/.test(script);
      return (anywhere && startsE2e) || (inSource && script.includes('run_bun_tests.mjs'));
    })
    .map(([name]) => name);
}

function readsReplays(directory) {
  return readdirSync(directory, {withFileTypes: true}).some(entry => {
    if (entry.name === 'node_modules' || entry.name === 'dist' || entry.name.startsWith('.')) {
      return false;
    }
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return readsReplays(path);
    return (
      /\.(ts|tsx|mts)$/.test(entry.name) &&
      !path.endsWith('derived.ts') &&
      readFileSync(path, 'utf8').includes(READER_IMPORT)
    );
  });
}

test('a script that starts a tool on the replays without the wrapper is named', () => {
  const manifest = {
    scripts: {
      e2e: 'node ../../node_modules/@playwright/test/cli.js test',
      test: 'node ../../tools/scripts/run_bun_tests.mjs ./src',
      build: 'tsc -b',
    },
  };
  assert.deepEqual(unwrapped(manifest, {anywhere: true, inSource: false}), ['e2e']);
  assert.deepEqual(unwrapped(manifest, {anywhere: true, inSource: true}), ['e2e', 'test']);
  assert.deepEqual(unwrapped(manifest, {anywhere: false, inSource: false}), []);
  const wrapped = {
    scripts: {e2e: `node ../../tools/scripts/with_derived_replays.mjs ${manifest.scripts.e2e}`},
  };
  assert.deepEqual(unwrapped(wrapped, {anywhere: true, inSource: true}), []);
});

test('every script of a package that reads replays starts its tool through the wrapper', () => {
  const offenders = workspaceLayout(WORKSPACE).packages.flatMap(({directory, manifest}) => {
    const root = join(WORKSPACE, directory);
    const anywhere = readsReplays(root);
    const inSource = existsSync(join(root, 'src')) && readsReplays(join(root, 'src'));
    return unwrapped(manifest, {anywhere, inSource}).map(name => `${directory} script ${name}`);
  });
  assert.deepEqual(
    offenders,
    [],
    'start these through tools/scripts/with_derived_replays.mjs: a bare run has no replays',
  );
});
