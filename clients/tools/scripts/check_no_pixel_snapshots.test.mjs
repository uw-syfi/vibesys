import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtemp, rm, writeFile} from 'node:fs/promises';
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

async function pixelSnapshotLines(t, parent, source) {
  const fixture = await mkdtemp(join(WORKSPACE_ROOT, parent, 'pixel-fixture-'));
  t.after(() => rm(fixture, {recursive: true, force: true}));
  const path = join(fixture, 'sample.spec.ts');
  await writeFile(path, source);
  const result = spawnSync(BIOME, ['lint', '--reporter=json', path], {
    cwd: WORKSPACE_ROOT,
    encoding: 'utf8',
  });
  assert.equal(result.error, undefined, result.error?.message);
  return JSON.parse(result.stdout)
    .diagnostics.filter(diagnostic => diagnostic.category === 'plugin')
    .filter(diagnostic => /Pixel snapshots are banned/.test(JSON.stringify(diagnostic)))
    .map(diagnostic => diagnostic.location.start.line);
}

const BANNED = [
  'await expect(page).toHaveScreenshot("a.png");',
  'await expect.soft(locator).toHaveScreenshot();',
  'await expect(page).not.toHaveScreenshot("a.png", {maxDiffPixels: 0});',
  'expect(image).toMatchSnapshot("a.png");',
  'expect(image).toMatchImageSnapshot();',
];

test('a pixel snapshot is a lint error in every place the clients keep tests', async t => {
  for (const parent of ['apps/ui-gallery/e2e', 'apps/desktop/src', 'libs/ui/src']) {
    const lines = await pixelSnapshotLines(t, parent, BANNED.join('\n'));
    assert.deepEqual(lines, [1, 2, 3, 4, 5], parent);
  }
});

test('measuring assertions are not flagged', async t => {
  const lines = await pixelSnapshotLines(
    t,
    'apps/ui-gallery/e2e',
    'await expect(box).toEqual({width: 1});\nawait page.screenshot({path: "x.png"});',
  );
  assert.deepEqual(lines, []);
});
