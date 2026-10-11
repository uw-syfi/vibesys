import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtemp, rm, writeFile} from 'node:fs/promises';
import {dirname, join, resolve} from 'node:path';
import test from 'node:test';
import {fileURLToPath} from 'node:url';

const WORKSPACE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
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
async function lintFixture(t, parent, source) {
  const fixture = await mkdtemp(join(WORKSPACE_ROOT, parent, 'fetch-fixture-'));
  t.after(() => rm(fixture, {recursive: true, force: true}));
  const path = join(fixture, 'sample.ts');
  await writeFile(path, source);
  return pluginDiagnostics(path);
}

const FETCHES = {
  call: "export const load = () => fetch('/api');\n",
  'globalThis.fetch': 'export const load = globalThis.fetch;\n',
  'window.fetch': "export const load = () => window.fetch('/api');\n",
};

for (const [name, source] of Object.entries(FETCHES)) {
  test(`web/src forbids ${name} outside the backend adapter`, async t => {
    assert.equal((await lintFixture(t, 'web/src', source)).length, 1);
  });
}

test('the backend adapter itself may fetch', () => {
  assert.deepEqual(pluginDiagnostics(join(WORKSPACE_ROOT, 'web/src/platform/backend.ts')), []);
});

test('code outside web/src is not held to the web rule', async t => {
  assert.deepEqual(await lintFixture(t, 'scripts', FETCHES.call), []);
});
