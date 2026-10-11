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
async function lintFixture(t, parent, source, name = 'sample.ts') {
  const fixture = await mkdtemp(join(WORKSPACE_ROOT, parent, 'fetch-fixture-'));
  t.after(() => rm(fixture, {recursive: true, force: true}));
  const path = join(fixture, name);
  await writeFile(path, source);
  return pluginDiagnostics(path);
}

const FETCHES = {
  call: "export const load = () => fetch('/api');\n",
  'globalThis.fetch': 'export const load = globalThis.fetch;\n',
  'window.fetch': "export const load = () => window.fetch('/api');\n",
  'self.fetch': "export const load = () => self.fetch('/api');\n",
  'a two-argument call': "export const load = () => fetch('/api', {method: 'POST'});\n",
  // Referencing the global, not only calling it: an alias reaches the network all the same.
  'an alias': "const request = fetch;\nexport const load = () => request('/api');\n",
};

for (const [name, source] of Object.entries(FETCHES)) {
  test(`apps/web/src forbids ${name} outside the backend adapter`, async t => {
    assert.equal((await lintFixture(t, 'apps/web/src', source)).length, 1);
  });
}

test('a feature package forbids fetch', async t => {
  assert.equal((await lintFixture(t, 'features/replay/src', FETCHES.call)).length, 1);
});

test('a property named fetch is not the global', async t => {
  const source = 'export const load = (client: {fetch(): void}) => client.fetch();\n';
  assert.deepEqual(await lintFixture(t, 'apps/web/src', source), []);
});

test('apps/web/src test files may fetch', async t => {
  assert.deepEqual(await lintFixture(t, 'apps/web/src', FETCHES.call, 'sample.test.ts'), []);
});

test('the backend adapter itself may fetch', () => {
  assert.deepEqual(pluginDiagnostics(join(WORKSPACE_ROOT, 'apps/web/src/platform/backend.ts')), []);
});

test('code outside the web app and the features is not held to the web rule', async t => {
  assert.deepEqual(await lintFixture(t, 'scripts', FETCHES.call), []);
});
