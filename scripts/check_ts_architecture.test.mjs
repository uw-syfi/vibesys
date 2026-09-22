import assert from 'node:assert/strict';
import {mkdir, mkdtemp, readFile, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import test from 'node:test';
import {fileURLToPath} from 'node:url';
import {cruise} from 'dependency-cruiser';
import extractDepcruiseOptions from 'dependency-cruiser/config-utl/extract-depcruise-options';
import {manifestErrors} from './check_ts_package_manifests.mjs';

const REPOSITORY_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const CONFIG = join(REPOSITORY_ROOT, '.dependency-cruiser.cjs');

test('dependency-cruiser rejects forbidden package and runtime edges', async () => {
  const root = await mkdtemp(join(tmpdir(), 'vibesys-dependency-rules-'));
  await writeFile(
    join(root, 'tsconfig.architecture.json'),
    await readFile(join(REPOSITORY_ROOT, 'tsconfig.architecture.json'), 'utf8'),
  );
  await writeSource(
    root,
    'backend-client',
    'index.ts',
    "import '../../core-state/src/index.js';\nimport '../../web/src/index.js';\n",
  );
  await writeSource(
    root,
    'core-state',
    'index.ts',
    "import 'node:fs';\nimport '@opentui/core';\nimport '../../tui/src/index.js';\nimport '../../web/src/index.js';\n",
  );
  await writeSource(
    root,
    'tui',
    'index.ts',
    "import '@vibesys/core-state';\nimport '@vibesys/core-state/private';\nimport './cycle.js';\nimport 'missing-package';\nimport 'undeclared-package';\n",
  );
  await writeSource(root, 'tui', 'cycle.ts', "import './index.js';\n");
  await writeSource(
    root,
    'web',
    'index.ts',
    "import 'node:fs';\nimport '@vibesys/tui';\nimport '@vibesys/backend-client';\n",
  );
  await writeExternalPackage(root, '@opentui/core');
  await writeExternalPackage(root, 'undeclared-package');

  const options = await extractDepcruiseOptions(CONFIG);
  const result = await cruise(
    ['clients/backend-client/src', 'clients/core-state/src', 'clients/tui/src', 'clients/web/src'],
    {
      ...options,
      baseDir: root,
      tsConfig: {fileName: join(root, 'tsconfig.architecture.json')},
    },
    {tsConfig: join(root, 'tsconfig.architecture.json')},
    {tsConfig: {options: {baseUrl: root}}},
  );
  const violatedRules = new Set(
    result.output.summary.violations.map(violation => violation.rule.name),
  );

  assert.deepEqual(
    violatedRules,
    new Set([
      'backend-client-is-lowest-layer',
      'core-state-does-not-depend-on-tui',
      'shared-state-does-not-depend-on-web',
      'frontends-are-independent',
      'web-has-no-node-runtime',
      'web-uses-browser-backend-client',
      'workspace-packages-use-public-exports',
      'core-state-has-no-node-runtime',
      'core-state-has-no-ui-runtime',
      'production-dependencies-are-declared',
      'no-circular-dependencies',
      'no-unresolvable-imports',
    ]),
  );
});

test('web can use the public browser export and shared state without private subpaths', async () => {
  const root = await mkdtemp(join(tmpdir(), 'vibesys-browser-rules-'));
  await writeFile(
    join(root, 'tsconfig.architecture.json'),
    await readFile(join(REPOSITORY_ROOT, 'tsconfig.architecture.json'), 'utf8'),
  );
  await writeSource(root, 'backend-client', 'browser.ts', 'export const client = {};\n');
  await writeSource(root, 'core-state', 'index.ts', 'export const state = {};\n');
  await writeSource(
    root,
    'web',
    'index.ts',
    "import {client} from '@vibesys/backend-client/browser';\nimport {state} from '@vibesys/core-state';\nimport './view.js';\nconsole.log(client, state);\n",
  );
  await writeSource(root, 'web', 'view.ts', 'export {};\n');
  const options = await extractDepcruiseOptions(CONFIG);
  const cruiseWeb = () =>
    cruise(
      ['clients/web/src'],
      {
        ...options,
        baseDir: root,
        tsConfig: {fileName: join(root, 'tsconfig.architecture.json')},
      },
      {tsConfig: join(root, 'tsconfig.architecture.json')},
      {tsConfig: {options: {baseUrl: root}}},
    );
  assert.deepEqual((await cruiseWeb()).output.summary.violations, []);

  await writeSource(root, 'web', 'view.ts', "import '@vibesys/backend-client/private';\n");
  assert.ok(
    (await cruiseWeb()).output.summary.violations.some(
      violation => violation.rule.name === 'no-unresolvable-imports',
    ),
  );
});

test('manifest policy rejects declared reverse dependencies', async () => {
  const root = await mkdtemp(join(tmpdir(), 'vibesys-manifest-rules-'));
  await writeManifest(root, 'backend-client', '@vibesys/backend-client', {
    '@vibesys/core-state': 'workspace:*',
    '@vibesys/web': 'workspace:*',
  });
  await writeManifest(root, 'core-state', '@vibesys/core-state', {
    '@vibesys/backend-client': 'workspace:*',
    '@opentui/core': '1.0.0',
  });
  await writeManifest(root, 'tui', '@vibesys/tui', {
    '@vibesys/backend-client': 'workspace:*',
  });
  await writeManifest(root, 'web', '@vibesys/web', {
    '@vibesys/backend-client': 'workspace:*',
    '@vibesys/core-state': 'workspace:*',
    '@vibesys/tui': 'workspace:*',
    '@opentui/core': '1.0.0',
  });

  assert.deepEqual(await manifestErrors(root), [
    'clients/backend-client/package.json: @vibesys/backend-client must not depend on @vibesys/core-state',
    'clients/backend-client/package.json: @vibesys/backend-client must not depend on @vibesys/web',
    'clients/core-state/package.json: @vibesys/core-state must not depend on @opentui/core',
    'clients/tui/package.json: @vibesys/tui must declare @vibesys/core-state in dependencies',
    'clients/web/package.json: @vibesys/web must not depend on @opentui/core',
    'clients/web/package.json: @vibesys/web must not depend on @vibesys/tui',
  ]);
});

test('manifest policy accepts independent frontends sharing client and state packages', async () => {
  const root = await mkdtemp(join(tmpdir(), 'vibesys-manifest-valid-'));
  await writeManifest(root, 'backend-client', '@vibesys/backend-client', {});
  await writeManifest(root, 'core-state', '@vibesys/core-state', {
    '@vibesys/backend-client': 'workspace:*',
  });
  for (const frontend of ['tui', 'web']) {
    await writeManifest(root, frontend, `@vibesys/${frontend}`, {
      '@vibesys/backend-client': 'workspace:*',
      '@vibesys/core-state': 'workspace:*',
    });
  }
  assert.deepEqual(await manifestErrors(root), []);
});

async function writeSource(root, packageDirectory, file, source) {
  const directory = join(root, 'clients', packageDirectory, 'src');
  await mkdir(directory, {recursive: true});
  await writeFile(join(directory, file), source);
}

async function writeManifest(root, directory, name, dependencies) {
  const packageDirectory = join(root, 'clients', directory);
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
