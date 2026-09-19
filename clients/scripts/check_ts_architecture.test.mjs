import assert from 'node:assert/strict';
import {mkdir, mkdtemp, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import test from 'node:test';
import {fileURLToPath} from 'node:url';
import {cruise} from 'dependency-cruiser';
import extractDepcruiseOptions from 'dependency-cruiser/config-utl/extract-depcruise-options';
import {manifestErrors} from './check_ts_package_manifests.mjs';

const WORKSPACE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const CONFIG = join(WORKSPACE_ROOT, '.dependency-cruiser.cjs');

test('dependency-cruiser rejects forbidden package and runtime edges', async () => {
  const root = await mkdtemp(join(tmpdir(), 'vibesys-dependency-rules-'));
  await writeFile(
    join(root, 'tsconfig.architecture.json'),
    JSON.stringify({
      compilerOptions: {
        baseUrl: '.',
        paths: {
          '@vibesys/backend-client': ['backend-client/src/index.ts'],
          '@vibesys/core-state': ['core-state/src/index.ts'],
          '@vibesys/tui': ['tui/src/index.ts'],
        },
      },
    }),
  );
  await writeSource(
    root,
    'backend-client',
    'index.ts',
    "import '../../core-state/src/index.js';\n",
  );
  await writeSource(
    root,
    'core-state',
    'index.ts',
    "import 'node:fs';\nimport '@opentui/core';\nimport '../../tui/src/index.js';\n",
  );
  await writeSource(
    root,
    'tui',
    'index.ts',
    "import '@vibesys/core-state';\nimport '@vibesys/core-state/private';\nimport './cycle.js';\nimport 'missing-package';\nimport 'undeclared-package';\n",
  );
  await writeSource(root, 'tui', 'cycle.ts', "import './index.js';\n");
  await writeExternalPackage(root, '@opentui/core');
  await writeExternalPackage(root, 'undeclared-package');

  const options = await extractDepcruiseOptions(CONFIG);
  const result = await cruise(['backend-client/src', 'core-state/src', 'tui/src'], {
    ...options,
    baseDir: root,
    tsConfig: {fileName: join(root, 'tsconfig.architecture.json')},
  });
  const violatedRules = new Set(
    result.output.summary.violations.map(violation => violation.rule.name),
  );

  assert.deepEqual(
    violatedRules,
    new Set([
      'backend-client-is-lowest-layer',
      'core-state-does-not-depend-on-tui',
      'workspace-packages-use-public-exports',
      'core-state-has-no-node-runtime',
      'core-state-has-no-ui-runtime',
      'production-dependencies-are-declared',
      'no-circular-dependencies',
      'no-unresolvable-imports',
    ]),
  );
});

test('manifest policy rejects declared reverse dependencies', async () => {
  const root = await mkdtemp(join(tmpdir(), 'vibesys-manifest-rules-'));
  await writeManifest(root, 'backend-client', '@vibesys/backend-client', {
    '@vibesys/core-state': 'workspace:*',
  });
  await writeManifest(root, 'core-state', '@vibesys/core-state', {
    '@vibesys/backend-client': 'workspace:*',
    '@opentui/core': '1.0.0',
  });
  await writeManifest(root, 'tui', '@vibesys/tui', {
    '@vibesys/backend-client': 'workspace:*',
  });

  assert.deepEqual(await manifestErrors(root), [
    'backend-client/package.json: @vibesys/backend-client must not depend on @vibesys/core-state',
    'core-state/package.json: @vibesys/core-state must not depend on @opentui/core',
    'tui/package.json: @vibesys/tui must declare @vibesys/core-state in dependencies',
  ]);
});

async function writeSource(root, packageDirectory, file, source) {
  const directory = join(root, packageDirectory, 'src');
  await mkdir(directory, {recursive: true});
  await writeFile(join(directory, file), source);
}

async function writeManifest(root, directory, name, dependencies) {
  const packageDirectory = join(root, directory);
  await mkdir(packageDirectory, {recursive: true});
  await writeFile(join(packageDirectory, 'package.json'), JSON.stringify({name, dependencies}));
}

async function writeExternalPackage(root, name) {
  const packageDirectory = join(root, 'node_modules', ...name.split('/'));
  await mkdir(packageDirectory, {recursive: true});
  await writeFile(
    join(packageDirectory, 'package.json'),
    JSON.stringify({name, version: '1.0.0', main: 'index.js'}),
  );
  await writeFile(join(packageDirectory, 'index.js'), 'export {};\n');
}
