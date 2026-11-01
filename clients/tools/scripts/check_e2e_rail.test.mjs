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

// Fixtures live under the desktop shell's e2e directory so the biome.json override glob matches
// them as it matches the specs.
async function lint(t, source) {
  const fixture = await mkdtemp(
    join(WORKSPACE_ROOT, 'apps', 'desktop-shell', 'e2e', 'rail-fixture-'),
  );
  t.after(() => rm(fixture, {recursive: true, force: true}));
  await mkdir(fixture, {recursive: true});
  const path = join(fixture, 'sample.ts');
  await writeFile(path, source);
  const result = spawnSync(BIOME, ['lint', '--reporter=json', path], {
    cwd: WORKSPACE_ROOT,
    encoding: 'utf8',
  });
  assert.equal(result.error, undefined, result.error?.message);
  return JSON.parse(result.stdout).diagnostics.filter(
    diagnostic => diagnostic.category === 'plugin',
  );
}

const FORBIDDEN = {
  'a press of the rail found through sidebar':
    "export const go = (page) => sidebar(page).getByRole('button', {name: 'Usage'}).click();\n",
  'a press of a rail kept in a variable named rail':
    "export const go = (rail) => rail.getByRole('button', {name: 'Usage'}).click();\n",
  'a rail found by its role and name':
    "export const find = (page) => page.getByRole('navigation', {name: 'VibeSys'});\n",
};

const ALLOWED = {
  'a press through showScreen': "export const go = (page) => showScreen(page, 'Usage');\n",
  'an assertion about the rail':
    "export const check = (page) => expect(sidebar(page).getByRole('button', {name: 'Usage'})).toBeVisible();\n",
  'a press of another navigation':
    "export const go = (page) => page.getByRole('navigation', {name: 'Settings sections'}).getByRole('button', {name: 'Machines'}).click();\n",
};

for (const [name, source] of Object.entries(FORBIDDEN)) {
  test(`the desktop e2e forbids ${name}`, async t => {
    const diagnostics = await lint(t, source);
    assert.equal(diagnostics.length, 1);
    assert.match(JSON.stringify(diagnostics[0]), /showScreen or pressMark/);
  });
}

for (const [name, source] of Object.entries(ALLOWED)) {
  test(`the desktop e2e allows ${name}`, async t => {
    assert.deepEqual(await lint(t, source), []);
  });
}
