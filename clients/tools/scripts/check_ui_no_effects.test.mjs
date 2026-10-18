import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdir, mkdtemp, rm, writeFile} from 'node:fs/promises';
import {dirname, join, resolve} from 'node:path';
import test from 'node:test';
import {fileURLToPath} from 'node:url';

const WORKSPACE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const BIOME = join(
  WORKSPACE_ROOT,
  'node_modules',
  '.bin',
  process.platform === 'win32' ? 'biome.cmd' : 'biome',
);

// Fixtures live under a real `ui/` directory inside the workspace so the biome.json override
// globs match them exactly as they match product code.
async function lint(t, directory, source, extension = 'ts') {
  const fixture = await mkdtemp(join(WORKSPACE_ROOT, 'tools', 'scripts', 'ui-effects-fixture-'));
  t.after(() => rm(fixture, {recursive: true, force: true}));
  const dir = join(fixture, directory);
  await mkdir(dir, {recursive: true});
  const sourcePath = join(dir, `sample.${extension}`);
  await writeFile(sourcePath, source);
  const result = spawnSync(BIOME, ['lint', '--reporter=json', sourcePath], {
    cwd: WORKSPACE_ROOT,
    encoding: 'utf8',
  });
  assert.equal(result.error, undefined, result.error?.message);
  const diagnostics = JSON.parse(result.stdout).diagnostics.filter(
    diagnostic => diagnostic.category === 'plugin',
  );
  return {status: result.status, diagnostics};
}

const FORBIDDEN = {
  fetch: "export const load = () => fetch('/api');\n",
  websocket: "export const open = () => new WebSocket('ws://x');\n",
  timeout: 'export const later = () => setTimeout(() => {}, 1);\n',
  interval: 'export const every = () => setInterval(() => {}, 1);\n',
  'transport import': "import {createClient} from '@vibesys/transport';\nexport {createClient};\n",
  // Regression: a statement pattern matched a named import of one specifier only.
  'multi-specifier import':
    "import {createClient, request} from '@vibesys/transport';\nexport {createClient, request};\n",
  're-export': "export {ipcRenderer} from 'electron';\n",
  'transport subpath type import':
    "import type {Foo} from '@vibesys/transport/websocket';\nexport type {Foo};\n",
  'namespace import': "import * as fs from 'node:fs';\nexport {fs};\n",
  'side-effect import': "import 'node:os';\n",
  'electron import': "import {ipcRenderer} from 'electron';\nexport {ipcRenderer};\n",
  'node import': "import {readFileSync} from 'node:fs';\nexport {readFileSync};\n",
};

for (const [name, source] of Object.entries(FORBIDDEN)) {
  test(`ui/ forbids ${name}`, async t => {
    const result = await lint(t, 'ui', source);
    assert.notEqual(result.status, 0);
    assert.equal(result.diagnostics.length, 1);
  });
}

test('ui/ forbids effect hooks in components', async t => {
  const source = [
    "import {useEffect, useLayoutEffect} from 'react';",
    'export function View() {',
    '  useEffect(() => {}, []);',
    '  useLayoutEffect(() => {}, []);',
    '  return null;',
    '}',
    '',
  ].join('\n');
  const result = await lint(t, 'ui', source, 'tsx');
  assert.equal(result.diagnostics.length, 2);
});

test('nested ui/ directories are covered', async t => {
  const result = await lint(t, join('pkg', 'src', 'ui', 'deep'), FORBIDDEN.fetch);
  assert.equal(result.diagnostics.length, 1);
});

test('presentational code and non-ui directories are allowed', async t => {
  const pure = 'export const label = (n: number) => `${n} items`;\n';
  const allowed = await lint(t, 'ui', pure);
  assert.equal(allowed.status, 0);
  assert.deepEqual(allowed.diagnostics, []);

  for (const source of Object.values(FORBIDDEN)) {
    const shell = await lint(t, 'shell', source);
    assert.deepEqual(shell.diagnostics, []);
  }
});

test('a ui-like name that is not a ui/ directory is not covered', async t => {
  const result = await lint(t, 'fluid', FORBIDDEN.fetch);
  assert.deepEqual(result.diagnostics, []);
});
