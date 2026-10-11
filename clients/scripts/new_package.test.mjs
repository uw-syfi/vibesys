import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readdirSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import {dirname, join, resolve} from 'node:path';
import test from 'node:test';
import {fileURLToPath} from 'node:url';
import {packageGraphErrors} from './check_package_graph.mjs';
import {createPackage, planPackage} from './new_package.mjs';
import {declarationErrors, workspaceLayout} from './workspace_layout.mjs';

const WORKSPACE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const TSC = join(WORKSPACE_ROOT, 'node_modules/typescript/bin/tsc');

// The fixture lives inside the workspace (a dot-directory, which no gate scans and git ignores) so
// the scaffolded projects resolve `typescript`, `@types/bun`, and `@types/node` from the
// workspace's own install, exactly as a real new package does.
function fixtureWorkspace(t) {
  const root = mkdtempSync(join(WORKSPACE_ROOT, '.vibesys-scaffold-test-'));
  t.after(() => rmSync(root, {recursive: true, force: true}));
  writeFileSync(
    join(root, 'pnpm-workspace.yaml'),
    'packages:\n  - "*"\n  - "features/*"\n  - "apps/*"\n',
  );
  copyFileSync(join(WORKSPACE_ROOT, 'tsconfig.base.json'), join(root, 'tsconfig.base.json'));
  writeFileSync(join(root, 'tsconfig.architecture.json'), '{"compilerOptions": {"paths": {}}}\n');
  return root;
}

const SCAFFOLDS = [
  ['feature', 'stop-run'],
  ['foundation', 'ui', '--runtime', 'dom'],
  ['service', 'connection', '--runtime', 'node'],
  ['core', 'core-thing'],
  ['app', 'web'],
];

test('every scaffolded package passes the graph and declaration checks', t => {
  const root = fixtureWorkspace(t);
  const directories = SCAFFOLDS.map(argv => createPackage(root, argv));
  assert.deepEqual(directories, [
    'features/stop-run',
    'ui',
    'connection',
    'core-thing',
    'apps/web',
  ]);
  assert.deepEqual(packageGraphErrors(root), []);
  assert.deepEqual(declarationErrors(root, workspaceLayout(root)), []);
});

test('every scaffolded package type-checks and passes its own tests', t => {
  const root = fixtureWorkspace(t);
  for (const argv of SCAFFOLDS) {
    const directory = join(root, createPackage(root, argv));
    const check = spawnSync(process.execPath, [TSC, '-b'], {cwd: directory, encoding: 'utf8'});
    assert.equal(check.status, 0, `${argv.join(' ')}: tsc -b\n${check.stdout}${check.stderr}`);
    const tests = spawnSync('bun', ['test', '--conditions=@vibesys/source', './src'], {
      cwd: directory,
      encoding: 'utf8',
    });
    assert.equal(tests.status, 0, `${argv.join(' ')}: bun test\n${tests.stdout}${tests.stderr}`);
  }
});

// Each case writes one module the entry's runtime does not provide and expects `tsc -b` to name it.
const CAPABILITY_VIOLATIONS = [
  {
    name: 'a renderer (dom) entry rejects a Node import',
    argv: ['feature', 'stop-run'],
    file: 'src/renderer/leak.ts',
    source: "export {readFileSync} from 'node:fs';\n",
    error: 'TS2307',
  },
  {
    name: 'a neutral entry rejects a Node import',
    argv: ['core', 'core-thing'],
    file: 'src/leak.ts',
    source: "export {readFileSync} from 'node:fs';\n",
    error: 'TS2307',
  },
  {
    name: 'a neutral entry rejects a Node global',
    argv: ['core', 'core-thing'],
    file: 'src/leak.ts',
    source: 'export const pid = process.pid;\n',
    error: 'TS2591',
  },
  {
    name: 'a neutral entry rejects a DOM global',
    argv: ['core', 'core-thing'],
    file: 'src/leak.ts',
    source: 'export const title = document.title;\n',
    error: 'TS2584',
  },
];

for (const {name, argv, file, source, error} of CAPABILITY_VIOLATIONS) {
  test(`${name} at compile time`, t => {
    const root = fixtureWorkspace(t);
    const directory = join(root, createPackage(root, argv));
    writeFileSync(join(directory, file), source);
    const check = spawnSync(process.execPath, [TSC, '-b'], {cwd: directory, encoding: 'utf8'});
    assert.notEqual(check.status, 0);
    assert.match(check.stdout, new RegExp(`${file.replace(/[./]/g, '\\$&')}.*${error}`));
  });
}

const REFUSALS = {
  'an unknown layer': ['widgets', 'thing'],
  'a name that is not kebab case': ['core', 'Thing'],
  'an unknown runtime': ['core', 'thing', '--runtime', 'deno'],
  'a runtime for a feature': ['feature', 'thing', '--runtime', 'dom'],
  'an unknown option': ['core', 'thing', '--layer', 'app'],
};

for (const [name, argv] of Object.entries(REFUSALS)) {
  test(`the scaffold refuses ${name} and writes nothing`, t => {
    const root = fixtureWorkspace(t);
    const before = readdirSync(root).sort();
    assert.throws(() => createPackage(root, argv));
    assert.deepEqual(readdirSync(root).sort(), before);
  });
}

test('the scaffold refuses a directory that already exists', t => {
  const root = fixtureWorkspace(t);
  mkdirSync(join(root, 'ui'));
  assert.throws(() => createPackage(root, ['foundation', 'ui']), /already exists/);
  assert.equal(existsSync(join(root, 'ui/package.json')), false);
});

test('a feature gets the three fixed entries and the feature- prefix', () => {
  assert.deepEqual(planPackage(['feature', 'resume-run']), {
    layer: 'feature',
    name: 'resume-run',
    packageName: '@vibesys/feature-resume-run',
    directory: 'features/resume-run',
    entries: {'./state': 'neutral', './renderer': 'dom', './main': 'node'},
  });
});
