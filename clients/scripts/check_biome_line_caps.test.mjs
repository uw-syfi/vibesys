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
const HARD_CAP_CONFIG = join(WORKSPACE_ROOT, 'biome.json');
const WARNING_CONFIG = join(WORKSPACE_ROOT, 'biome.warnings.json');

function sourceWithLines(count) {
  return `${';\n'.repeat(count - 1)}export {};\n`;
}

async function lint(t, source, fileName, config) {
  const fixture = await mkdtemp(join(WORKSPACE_ROOT, 'scripts', 'line-cap-fixture-'));
  t.after(() => rm(fixture, {recursive: true, force: true}));
  const sourcePath = join(fixture, fileName);
  await writeFile(sourcePath, source);
  const result = spawnSync(
    BIOME,
    [
      'lint',
      '--reporter=json',
      `--config-path=${config}`,
      '--only=style/noExcessiveLinesPerFile',
      sourcePath,
    ],
    {cwd: WORKSPACE_ROOT, encoding: 'utf8'},
  );
  assert.equal(result.error, undefined, result.error?.message);
  return {status: result.status, report: JSON.parse(result.stdout)};
}

function lineCapDiagnostics(result) {
  return result.report.diagnostics.filter(
    diagnostic => diagnostic.category === 'lint/style/noExcessiveLinesPerFile',
  );
}

test('production files warn before the hard limit and fail at the hard limit', async t => {
  const warning = await lint(t, sourceWithLines(1_501), 'synthetic.ts', WARNING_CONFIG);
  assert.equal(warning.status, 0);
  assert.deepEqual(
    lineCapDiagnostics(warning).map(diagnostic => diagnostic.severity),
    ['warning'],
  );

  const error = await lint(t, sourceWithLines(2_001), 'synthetic.ts', HARD_CAP_CONFIG);
  assert.notEqual(error.status, 0);
  assert.deepEqual(
    lineCapDiagnostics(error).map(diagnostic => diagnostic.severity),
    ['error'],
  );
});

test('test files have an explicit finite hard limit', async t => {
  const below = await lint(t, sourceWithLines(10_000), 'synthetic.test.ts', HARD_CAP_CONFIG);
  assert.equal(below.status, 0);
  assert.deepEqual(lineCapDiagnostics(below), []);

  const above = await lint(t, sourceWithLines(10_001), 'synthetic.test.ts', HARD_CAP_CONFIG);
  assert.notEqual(above.status, 0);
  assert.deepEqual(
    lineCapDiagnostics(above).map(diagnostic => diagnostic.severity),
    ['error'],
  );
});
