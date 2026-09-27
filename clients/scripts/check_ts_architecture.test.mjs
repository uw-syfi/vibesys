import assert from 'node:assert/strict';
import {mkdir, mkdtemp, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import test from 'node:test';
import {fileURLToPath} from 'node:url';
import {cruise} from 'dependency-cruiser';
import extractDepcruiseOptions from 'dependency-cruiser/config-utl/extract-depcruise-options';
import extractTSConfig from 'dependency-cruiser/config-utl/extract-ts-config';
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
          '@vibesys/web': ['web/src/index.ts'],
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
    "import '@vibesys/core-state';\nimport '@vibesys/core-state/private';\nimport './cycle-a.js';\nimport 'missing-package';\nimport 'undeclared-package';\n",
  );
  await writeSource(root, 'tui', 'cycle-a.ts', "import './cycle-b.js';\n");
  await writeSource(root, 'tui', 'cycle-b.ts', "import './cycle-a.js';\n");
  await writeSource(root, 'web', 'index.ts', "import '@vibesys/core-state';\n");
  await writeExternalPackage(root, '@opentui/core');
  await writeExternalPackage(root, 'undeclared-package');

  const options = await extractDepcruiseOptions(CONFIG);
  const result = await cruise(['backend-client/src', 'core-state/src', 'tui/src', 'web/src'], {
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

// Each case is the smallest fixture that breaks one rule. The fixture root also has the valid
// edges the rule must keep allowing, so a rule that over-matches fails the `clean` case.
const VALID_FILES = {
  'backend-client/src/index.ts': '',
  'core-state/src/index.ts': "import '@vibesys/backend-client';\n",
  'tui/src/index.ts':
    "import '@opentui/core';\nimport '@vibesys/core-state';\nimport './runtime.js';\nimport './ui/app.js';\nimport './session-controller.js';\n",
  'web/src/index.ts': "import '@vibesys/backend-client';\nimport '@vibesys/core-state';\n",
  'tui/src/runtime.ts': "import '@opentui/core';\nimport type {} from './session-controller.js';\n",
  'tui/src/session-controller.ts': "import './session-model.js';\nimport './ui/theme.js';\n",
  'tui/src/session-model.ts': "import './ui/theme.js';\n",
  'tui/src/launcher.ts': "import './ui/theme.js';\nimport 'node:fs';\n",
  'tui/src/ui/theme.ts': '',
  'tui/src/ui/app.ts':
    "import '@opentui/core';\nimport type {} from '../session-controller.js';\nimport '../session-model.js';\n",
  'tui/src/ui/app.test.ts': "import '../session-controller.js';\nimport '../runtime.js';\n",
  'tui/dev/harness.ts':
    "import '@vibesys/core-state';\nimport '@vibesys/backend-client';\nimport 'declared-package';\n",
  'tui/benchmarks/bench.ts': "import '../src/session-model.js';\n",
  'scripts/check.mjs': "import 'node:fs';\nimport 'declared-package';\n",
};

const RULE_CASES = [
  {
    rule: 'shipping-path-does-not-depend-on-dev-harness',
    files: {'tui/src/ui/theme.ts': "import '../../dev/harness.js';\n"},
  },
  {
    rule: 'shipping-path-does-not-depend-on-dev-harness',
    files: {'core-state/src/index.ts': "import '../../tui/dev/harness.js';\n"},
  },
  {
    rule: 'backend-client-neutral-has-no-node-runtime',
    files: {'backend-client/src/neutral.ts': "import 'node:crypto';\n"},
  },
  {
    rule: 'tools-are-leaves',
    files: {'tui/src/session-model.ts': "import '../benchmarks/bench.js';\n"},
  },
  {
    rule: 'tools-are-leaves',
    files: {'tui/dev/harness.ts': "import '../../scripts/check.mjs';\n"},
  },
  {
    rule: 'scripts-do-not-import-package-code',
    files: {'scripts/check.mjs': "import '../tui/src/session-model.js';\n"},
  },
  {
    rule: 'production-code-does-not-import-tests',
    files: {'tui/src/session-model.ts': "import './ui/app.test.js';\n"},
  },
  {
    rule: 'workspace-packages-use-public-exports',
    files: {'tui/dev/harness.ts': "import '../../core-state/src/index.js';\n"},
  },
  {
    rule: 'workspace-packages-have-no-deep-imports',
    files: {
      'tui/dev/harness.ts': "import '@vibesys/core-state/dist/index.js';\n",
      'node_modules/@vibesys/core-state/dist/index.js': 'export {};\n',
      'node_modules/@vibesys/core-state/package.json': '{"name":"@vibesys/core-state"}',
    },
  },
  {
    rule: 'workspace-packages-have-no-deep-imports',
    files: {
      'scripts/check.mjs': "import '@vibesys/tui/src/index.js';\n",
      'node_modules/@vibesys/tui/src/index.js': 'export {};\n',
      'node_modules/@vibesys/tui/package.json': '{"name":"@vibesys/tui"}',
    },
  },
  {
    rule: 'no-unresolvable-imports',
    files: {'scripts/check.mjs': "import 'missing-package';\n"},
  },
  {
    rule: 'no-circular-dependencies',
    files: {
      'tui/dev/a.ts': "import './b.js';\n",
      'tui/dev/b.ts': "import './a.js';\n",
    },
  },
  {
    rule: 'production-dependencies-are-declared',
    files: {'tui/dev/harness.ts': "import 'undeclared-package';\n"},
  },
  {
    rule: 'production-dependencies-are-declared',
    files: {'scripts/check.mjs': "import 'undeclared-package';\n"},
  },
  {
    rule: 'tui-opentui-is-confined-to-ui-and-composition-root',
    files: {'tui/src/session-model.ts': "import '@opentui/core';\n"},
  },
  {
    rule: 'tui-state-does-not-depend-on-controller-or-wiring',
    files: {'tui/src/session-model.ts': "import type {} from './session-controller.js';\n"},
  },
  {
    rule: 'tui-state-does-not-depend-on-controller-or-wiring',
    files: {'tui/src/session-controller.ts': "import './runtime.js';\n"},
  },
  {
    rule: 'tui-ui-does-not-depend-on-composition-root',
    files: {'tui/src/ui/app.ts': "import type {} from '../runtime.js';\n"},
  },
  {
    rule: 'tui-ui-uses-controller-by-type-only',
    files: {'tui/src/ui/app.ts': "import '../session-controller.js';\n"},
  },
  {
    rule: 'tui-entrypoints-are-not-imported',
    files: {'tui/src/ui/app.ts': "import type {} from '../index.js';\n"},
  },
  {
    rule: 'tui-entrypoints-are-not-imported',
    files: {'tui/dev/harness.ts': "import '../src/launcher.js';\n"},
  },
  {
    rule: 'tui-launcher-stays-standalone',
    files: {'tui/src/launcher.ts': "import './session-model.js';\n"},
  },
  {
    rule: 'tui-launcher-stays-standalone',
    files: {'tui/src/launcher.ts': "import '@opentui/core';\n"},
  },
  {
    rule: 'tui-launcher-stays-standalone',
    files: {'tui/src/launcher.ts': "import '@vibesys/core-state';\n"},
  },
];

async function violatedRules(files) {
  const root = await mkdtemp(join(tmpdir(), 'vibesys-layer-rules-'));
  await writeFile(
    join(root, 'tsconfig.architecture.json'),
    JSON.stringify({
      compilerOptions: {
        baseUrl: '.',
        paths: {
          '@vibesys/backend-client': ['backend-client/src/index.ts'],
          '@vibesys/core-state': ['core-state/src/index.ts'],
          '@vibesys/tui': ['tui/src/index.ts'],
          '@vibesys/web': ['web/src/index.ts'],
        },
      },
    }),
  );
  await writeExternalPackage(root, '@opentui/core');
  await writeExternalPackage(root, 'declared-package');
  await writeExternalPackage(root, 'undeclared-package');
  await writeFile(
    join(root, 'package.json'),
    JSON.stringify({devDependencies: {'declared-package': '1.0.0', '@opentui/core': '1.0.0'}}),
  );
  for (const [file, source] of Object.entries(files)) {
    await mkdir(dirname(join(root, file)), {recursive: true});
    await writeFile(join(root, file), source);
  }
  const options = await extractDepcruiseOptions(CONFIG);
  const tsConfigFile = join(root, 'tsconfig.architecture.json');
  const result = await cruise(
    [
      'backend-client/src',
      'core-state/src',
      'tui/src',
      'tui/dev',
      'tui/benchmarks',
      'web/src',
      'scripts',
    ],
    {
      ...options,
      baseDir: root,
      // The resolver reads the tsconfig `paths` aliases from the rule set's options.
      ruleSet: {...options.ruleSet, options: {tsConfig: {fileName: tsConfigFile}}},
    },
    undefined,
    {tsConfig: extractTSConfig(tsConfigFile)},
  );
  return result.output.summary.violations.map(violation => violation.rule.name);
}

test('valid layering produces no violations', async () => {
  assert.deepEqual(await violatedRules(VALID_FILES), []);
});

for (const {rule, files} of RULE_CASES) {
  test(`${rule} rejects ${Object.keys(files)[0]}: ${Object.values(files)[0].trim()}`, async () => {
    assert.ok((await violatedRules({...VALID_FILES, ...files})).includes(rule));
  });
}

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
  await writeManifest(root, 'web', '@vibesys/web', {
    '@vibesys/backend-client': 'workspace:*',
    '@vibesys/core-state': 'workspace:*',
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
