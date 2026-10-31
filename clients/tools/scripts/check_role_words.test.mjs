import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtemp, readFile, rm, writeFile} from 'node:fs/promises';
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
const CATALOG = join(WORKSPACE_ROOT, 'libs/contracts/src/generated/display-names.generated.ts');

/** Every distinct name the catalog gives a role, read from the generated module's source text. */
async function catalogTitles() {
  const text = await readFile(CATALOG, 'utf8');
  const roles = text.slice(text.indexOf('"roles"'), text.indexOf('} as const'));
  return [...new Set([...roles.matchAll(/": "([^"]+)"/g)].map(match => match[1]))];
}

// A fixture lives in a real package path so it meets the same Biome configuration as product code.
async function roleWordDiagnostics(t, source) {
  const fixture = await mkdtemp(join(WORKSPACE_ROOT, 'libs/contracts/src', 'role-words-fixture-'));
  t.after(() => rm(fixture, {recursive: true, force: true}));
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

test('no client source spells the name of any role the catalog declares', async t => {
  const titles = await catalogTitles();
  assert.ok(titles.length > 0);
  for (const title of titles) {
    const diagnostics = await roleWordDiagnostics(t, `export const label = '${title}';\n`);
    assert.equal(diagnostics.length, 1, `the rule lets "${title}" through`);
    assert.match(JSON.stringify(diagnostics[0]), /no-role-words/);
  }
});

test('the names a role had before are refused too, whole or inside text', async t => {
  for (const source of [
    "export const label = 'Implementer';\n",
    'export const label = `Judge returned ${verdict}`;\n',
    "export const label = 'the judge read it';\n",
  ]) {
    const diagnostics = await roleWordDiagnostics(t, source);
    assert.equal(diagnostics.length, 1, source);
  }
});

test('a role id is not a name and stays allowed', async t => {
  const diagnostics = await roleWordDiagnostics(
    t,
    "export const ids = ['judge', 'multi-implementer', 'judge_result', 'perf-evaluator'];\n",
  );
  assert.deepEqual(diagnostics, []);
});
