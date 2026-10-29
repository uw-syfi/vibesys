import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtemp, rm, writeFile} from 'node:fs/promises';
import {dirname, join, resolve} from 'node:path';
import test from 'node:test';
import {fileURLToPath} from 'node:url';

// The control-state rules (docs/contributing/clients-architecture.md, "State"), as wired in
// biome.json: a feature's state/ and effects/ schedule no timers and abort nothing by hand, and a
// control-state package does not run its control state on the reducer kernel.

const WORKSPACE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const BIOME = join(
  WORKSPACE_ROOT,
  'node_modules',
  '.bin',
  process.platform === 'win32' ? 'biome.cmd' : 'biome',
);

function pluginDiagnostics(path) {
  const result = spawnSync(BIOME, ['lint', '--reporter=json', path], {
    cwd: WORKSPACE_ROOT,
    encoding: 'utf8',
  });
  assert.equal(result.error, undefined, result.error?.message);
  return JSON.parse(result.stdout).diagnostics.filter(
    diagnostic => diagnostic.category === 'plugin',
  );
}

// Fixtures live inside a real directory so the biome.json override globs match them exactly as
// they match product code.
async function lintFixture(t, parent, source, name = 'sample.ts') {
  const fixture = await mkdtemp(join(WORKSPACE_ROOT, parent, 'control-fixture-'));
  t.after(() => rm(fixture, {recursive: true, force: true}));
  const path = join(fixture, name);
  await writeFile(path, source);
  return pluginDiagnostics(path);
}

const HAND_ROLLED = {
  'a setTimeout call': 'export const later = () => setTimeout(() => undefined, 10);\n',
  'a setInterval call': 'export const tick = () => setInterval(() => undefined, 10);\n',
  'an AbortController': 'export const abort = () => new AbortController();\n',
  'an alias of setTimeout':
    'const later = setTimeout;\nexport const run = () => later(() => 0, 1);\n',
  'globalThis.setTimeout': 'export const later = () => globalThis.setTimeout(() => 0, 1);\n',
};

for (const directory of ['features/run-page/src/state', 'features/run-page/src/renderer/effects']) {
  for (const [name, source] of Object.entries(HAND_ROLLED)) {
    test(`${directory} forbids ${name}`, async t => {
      assert.equal((await lintFixture(t, directory, source)).length, 1);
    });
  }
}

test("a feature's state tests may use timers", async t => {
  const source = HAND_ROLLED['a setTimeout call'];
  assert.deepEqual(
    await lintFixture(t, 'features/run-page/src/state', source, 'sample.test.ts'),
    [],
  );
});

test('code outside state/ and effects/ is not held to the feature rule', async t => {
  assert.deepEqual(
    await lintFixture(t, 'libs/run-session/src', HAND_ROLLED['an AbortController']),
    [],
  );
});

const KERNEL = {
  'a createStore call':
    "import {createStore, only} from '@vibesys/store';\n" +
    'export const store = createStore(() => only(0), 0, () => undefined);\n',
  'an alias of createStore':
    "import {createStore} from '@vibesys/store';\nexport const make = createStore;\n",
};

for (const directory of ['features/run-control/src/main', 'libs/run-session/src']) {
  for (const [name, source] of Object.entries(KERNEL)) {
    test(`${directory} forbids ${name}`, async t => {
      assert.equal((await lintFixture(t, directory, source)).length, 1);
    });
  }
}

test("a control-state package's fold module may run its fold on the reducer kernel", () => {
  assert.deepEqual(
    pluginDiagnostics(join(WORKSPACE_ROOT, 'libs', 'run-session', 'src', 'run.ts')),
    [],
  );
});

test('a feature still on the reducer kernel is not held to the control-state rule', async t => {
  assert.deepEqual(
    await lintFixture(t, 'features/app-shell/src/renderer/effects', KERNEL['a createStore call']),
    [],
  );
});
