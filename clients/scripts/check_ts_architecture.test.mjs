import assert from 'node:assert/strict';
import {execFileSync} from 'node:child_process';
import {mkdir, mkdtemp, rm, symlink, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import test from 'node:test';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {cruise} from 'dependency-cruiser';
import extractDepcruiseOptions from 'dependency-cruiser/config-utl/extract-depcruise-options';
import extractTSConfig from 'dependency-cruiser/config-utl/extract-ts-config';
import {cruiseWorkspace} from './check_ts_architecture.mjs';
import {manifestErrors} from './check_ts_package_manifests.mjs';
import {declarationErrors, pathAlternation, workspaceLayout} from './workspace_layout.mjs';

const WORKSPACE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const CONFIG = join(WORKSPACE_ROOT, '.dependency-cruiser.mjs');

// `.dependency-cruiser.mjs` derives its path patterns from the workspace on disk, so a fixture
// must use this repository's own directory names or no rule can match it. Deriving the fixture's
// shape from the same module is what keeps the two in step: a package or a scan root that joins
// the workspace is covered without anyone editing a list here.
const REPOSITORY_LAYOUT = workspaceLayout(WORKSPACE_ROOT);
const PACKAGE_DIRECTORIES = REPOSITORY_LAYOUT.packages.map(({directory}) => directory);

test('dependency-cruiser rule names are unique', async () => {
  const options = await extractDepcruiseOptions(CONFIG);
  const names = options.ruleSet.forbidden.map(rule => rule.name);
  assert.equal(new Set(names).size, names.length);
});

test('dependency-cruiser rejects forbidden package and runtime edges', async t => {
  const root = await scratchDirectory(t, 'vibesys-dependency-rules-');
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
      'core-state-is-below-frontends',
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
// edges the rule must keep allowing, so a rule that over-matches fails the `clean` case. The
// fixtures use the real package names, because `.dependency-cruiser.mjs` derives its path
// patterns from the workspace on disk.
const VALID_FILES = {
  'backend-client/src/index.ts': '',
  'backend-client/src/backoff.test.ts': "import './test-support/expect.js';\n",
  'backend-client/src/test-support/expect.ts': "import 'node:module';\n",
  'backend-client/src/testing/fake-clock.test-helper.ts': '',
  'backend-client/src/testing/fake-clock.test.ts':
    "import '../test-support/expect.js';\nimport './fake-clock.test-helper.js';\n",
  'core-state/src/index.ts': "import '@vibesys/backend-client';\n",
  'tui/src/index.ts':
    "import '@opentui/core';\nimport '@vibesys/core-state';\nimport './runtime.js';\nimport './ui/app.js';\nimport './session-controller.js';\n",
  'web/src/index.ts': "import '@vibesys/backend-client';\nimport '@vibesys/core-state';\n",
  // A leaf tool reaching its own directory is the edge `tools-are-leaves` must keep allowing, and
  // the only thing its `$1` back-reference permits.
  'web/e2e/live.spec.ts': "import 'declared-package';\nimport './fixtures.js';\n",
  'web/e2e/fixtures.ts': '',
  'tui/src/runtime.ts': "import '@opentui/core';\nimport type {} from './session-controller.js';\n",
  'tui/src/session-controller.ts': "import './session-model.js';\nimport './theme.js';\n",
  'tui/src/session-model.ts': "import './theme.js';\n",
  'tui/src/launcher.ts': "import './theme.js';\nimport 'node:fs';\n",
  'tui/src/theme.ts': '',
  'tui/src/ui/app.ts':
    "import '@opentui/core';\nimport type {} from '../session-controller.js';\nimport '../session-model.js';\nimport '../theme.js';\n",
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
    rule: 'tools-are-leaves',
    files: {'web/src/index.ts': "import '../e2e/live.spec.js';\n"},
  },
  {
    // One leaf tool reaching another. Excluding every tool from `from` rather than only the
    // importer's own directory permits this, so this case fails without the `$1` back-reference.
    rule: 'tools-are-leaves',
    files: {'web/e2e/live.spec.ts': "import '../../scripts/check.mjs';\n"},
  },
  {
    rule: 'tools-are-leaves',
    files: {'web/e2e/live.spec.ts': "import '../../tui/benchmarks/bench.js';\n"},
  },
  {
    rule: 'tools-are-leaves',
    files: {'core-state/bench/run.ts': "import '../../web/e2e/fixtures.js';\n"},
  },
  {
    rule: 'backend-client-is-lowest-layer',
    files: {'backend-client/src/index.ts': "import '../../web/src/index.js';\n"},
  },
  {
    rule: 'web-does-not-depend-on-peer-frontends',
    files: {'web/src/index.ts': "import '../../tui/src/session-model.js';\n"},
  },
  {
    rule: 'tui-does-not-depend-on-peer-frontends',
    files: {'tui/src/session-model.ts': "import '../../web/src/index.js';\n"},
  },
  {
    rule: 'core-state-is-below-frontends',
    files: {'core-state/src/index.ts': "import '../../web/src/index.js';\n"},
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
    rule: 'production-code-does-not-import-backend-client-test-support',
    files: {'backend-client/src/index.ts': "import './test-support/expect.js';\n"},
  },
  {
    rule: 'production-code-does-not-import-backend-client-test-support',
    files: {
      'backend-client/src/index.ts': "import './testing/fake-clock.test-helper.js';\n",
    },
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
    // The rule could not reach the end-to-end specs while the scan roots were a hand-written
    // list that omitted `web/e2e`, which is how `@playwright/test` stayed undeclared.
    rule: 'production-dependencies-are-declared',
    files: {'web/e2e/live.spec.ts': "import 'undeclared-package';\n"},
  },
  {
    rule: 'tui-ui-does-not-depend-on-backend-client',
    files: {'tui/src/ui/app.ts': "import type {} from '@vibesys/backend-client';\n"},
  },
  {
    rule: 'dev-cannot-deep-import-src',
    files: {'tui/dev/harness.ts': "import '../src/session-model.js';\n"},
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
    rule: 'state-does-not-import-src/ui',
    files: {
      'tui/src/session-model.ts': "import './ui/shared.js';\n",
      'tui/src/ui/shared.ts': '',
    },
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

async function violatedRules(t, files) {
  const root = await workspaceFixture(t, 'vibesys-layer-rules-');
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
  const declared = {devDependencies: {'declared-package': '1.0.0', '@opentui/core': '1.0.0'}};
  await writeFile(join(root, 'package.json'), JSON.stringify(declared));
  // Each package declares the same externals, because `production-dependencies-are-declared`
  // resolves a declaration against the manifest nearest the importing module.
  for (const directory of PACKAGE_DIRECTORIES) {
    await writePackage(root, directory, `@vibesys/${directory}`, ['src'], declared);
  }
  for (const [file, source] of Object.entries(files)) {
    await mkdir(dirname(join(root, file)), {recursive: true});
    await writeFile(join(root, file), source);
  }
  const options = await extractDepcruiseOptions(CONFIG);
  const tsConfigFile = join(root, 'tsconfig.architecture.json');
  // Derived, not listed: a rule case in a directory the layout finds must be cruised without
  // anyone adding it here, which is the gap that let `web/e2e` go unscanned.
  const result = await cruise(
    workspaceLayout(root).scanRoots,
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

test('valid layering produces no violations', async t => {
  assert.deepEqual(await violatedRules(t, VALID_FILES), []);
});

for (const {rule, files} of RULE_CASES) {
  test(`${rule} rejects ${Object.keys(files)[0]}: ${Object.values(files)[0].trim()}`, async t => {
    assert.ok((await violatedRules(t, {...VALID_FILES, ...files})).includes(rule));
  });
}

test('manifest policy rejects declared reverse dependencies', async t => {
  const root = await workspaceFixture(t, 'vibesys-manifest-rules-');
  await writeManifest(root, 'backend-client', '@vibesys/backend-client', {
    '@vibesys/core-state': 'workspace:*',
  });
  await writeManifest(
    root,
    'core-state',
    '@vibesys/core-state',
    {'@vibesys/backend-client': 'workspace:*', '@opentui/core': '1.0.0'},
    {pretest: 'pnpm --filter @vibesys/core-state^... build'},
  );
  await writeManifest(
    root,
    'tui',
    '@vibesys/tui',
    {'@vibesys/backend-client': 'workspace:*'},
    {pretest: 'pnpm --filter @vibesys/tui^... build'},
  );
  await writeManifest(
    root,
    'web',
    '@vibesys/web',
    {'@vibesys/backend-client': 'workspace:*', '@vibesys/core-state': 'workspace:*'},
    {pretest: 'pnpm --filter @vibesys/web^... build'},
  );

  assert.deepEqual(manifestErrors(root), [
    'backend-client/package.json: @vibesys/backend-client must not depend on @vibesys/core-state',
    'core-state/package.json: @vibesys/core-state must not depend on @opentui/core',
    'tui/package.json: @vibesys/tui must declare @vibesys/core-state in dependencies',
  ]);
});

test('manifest policy names a package whose tests do not build its dependencies', async t => {
  const root = await workspaceFixture(t, 'vibesys-manifest-pretest-');
  // `pnpm -r run test` runs these two in order and builds neither, so `core-state`'s suite reads
  // whatever `backend-client/dist` the last build in the checkout left behind. `@vibesys/web`
  // declared no `pretest` at all while importing values from `@vibesys/core-state` (#1039).
  await writeManifest(root, 'backend-client', '@vibesys/backend-client', {});
  await writeManifest(root, 'core-state', '@vibesys/core-state', {
    '@vibesys/backend-client': 'workspace:*',
  });
  // A correctly wired dependent, so the rule cannot pass by rejecting every package.
  await writeManifest(
    root,
    'tui',
    '@vibesys/tui',
    {'@vibesys/backend-client': 'workspace:*', '@vibesys/core-state': 'workspace:*'},
    {pretest: 'pnpm --filter @vibesys/tui^... build'},
  );

  // `backend-client` has no workspace dependency to build, so it needs no hook and gets no error.
  assert.deepEqual(manifestErrors(root), [
    'core-state/package.json: @vibesys/core-state must declare "pretest": ' +
      '"pnpm --filter @vibesys/core-state^... build", because `pnpm -r test` runs the packages ' +
      'in order but builds none of them',
  ]);
});

test('manifest policy names a package that joins the workspace without one', async t => {
  const root = await workspaceFixture(t, 'vibesys-manifest-policy-');
  await writeManifest(root, 'stub', '@vibesys/stub', {});

  assert.deepEqual(manifestErrors(root), [
    'stub/package.json: @vibesys/stub has no dependency policy in ' +
      'scripts/check_ts_package_manifests.mjs',
  ]);
});

// The workspace admits directories, so the layout is what every gate enumerates from: a package
// or a directory that joins it must appear here without anyone editing a list. These fixtures are
// plain directories with no git metadata, which is the unpacked-sdist case: the layout cannot ask
// git what is ignored and falls back to excluding build and install output.
const TOOLING_DIRECTORY_SETS = [[], ['bench'], ['e2e'], ['dev', 'e2e']];

for (const tooling of TOOLING_DIRECTORY_SETS) {
  test(`workspace layout covers a package whose tools are [${tooling.join(' ')}]`, async t => {
    const root = await workspaceFixture(t, 'vibesys-layout-');
    // Directories that hold no source must stay out, whether or not a build has run.
    await writePackage(root, 'stub', '@vibesys/stub', [
      'src',
      ...tooling,
      'dist',
      'node_modules',
      '.cache',
    ]);
    await mkdir(join(root, 'scripts'), {recursive: true});

    const layout = workspaceLayout(root);

    assert.deepEqual(
      layout.packages.map(({name, directory}) => ({name, directory})),
      [{name: '@vibesys/stub', directory: 'stub'}],
    );
    assert.deepEqual(
      layout.toolingDirectories,
      [...tooling.map(directory => `stub/${directory}`).sort(), 'scripts'].sort(),
    );
    assert.deepEqual(
      layout.scanRoots,
      ['scripts', 'stub/src', ...tooling.map(directory => `stub/${directory}`)].sort(),
    );
  });
}

test('workspace layout leaves out a directory git ignores', async t => {
  const root = await workspaceFixture(t, 'vibesys-layout-ignored-');
  // `coverage` and `build` are ordinary local tool output: gitignored, untracked, and named by
  // neither the build-output fallback nor any list in the gates. Only asking git keeps them out.
  await writeFile(join(root, '.gitignore'), 'coverage/\nbuild/\n');
  execFileSync('git', ['init', '--quiet'], {cwd: root});
  await writePackage(root, 'stub', '@vibesys/stub', ['src', 'e2e', 'coverage', 'build']);

  assert.deepEqual(workspaceLayout(root).scanRoots, ['stub/e2e', 'stub/src']);
});

test('workspace layout covers a symlinked package and a symlinked tool directory', async t => {
  const root = await workspaceFixture(t, 'vibesys-layout-symlink-');
  const outside = await scratchDirectory(t, 'vibesys-layout-target-');
  await writePackage(outside, 'linked', '@vibesys/linked', ['src']);
  await mkdir(join(outside, 'shared-e2e'), {recursive: true});
  await writePackage(root, 'stub', '@vibesys/stub', ['src']);
  // `pnpm -r` resolves a symlinked directory and runs the linked package's scripts, so a layout
  // that skipped it would build, check, and test a package no gate can see.
  await symlink(join(outside, 'linked'), join(root, 'linked'));
  await symlink(join(outside, 'shared-e2e'), join(root, 'stub/e2e'));
  await symlink(join(outside, 'absent'), join(root, 'dangling'));

  const layout = workspaceLayout(root);

  assert.deepEqual(
    layout.packages.map(({name, directory}) => ({name, directory})),
    [
      {name: '@vibesys/linked', directory: 'linked'},
      {name: '@vibesys/stub', directory: 'stub'},
    ],
  );
  assert.deepEqual(layout.scanRoots, ['linked/src', 'stub/e2e', 'stub/src']);
});

test('a symlinked package costs only its own subtree the git answer', async t => {
  const root = await workspaceFixture(t, 'vibesys-layout-symlink-git-');
  const outside = await scratchDirectory(t, 'vibesys-layout-target-');
  await writeFile(join(root, '.gitignore'), 'coverage/\nbuild/\n');
  execFileSync('git', ['init', '--quiet'], {cwd: root});
  await writePackage(root, 'stub', '@vibesys/stub', ['src', 'coverage', 'build']);
  await writePackage(outside, 'linked', '@vibesys/linked', ['src', 'coverage']);
  // `git check-ignore` answers for no path beyond a symlink and rejects the whole pathspec with
  // exit 128 rather than skipping the offender, so asking about both subtrees in one batch put
  // `stub/build` and `stub/coverage` back among the scan roots. This is the interaction the two
  // features hid from each other: the git case had no symlink and the symlink case had no `git
  // init`, so each passed while together they disabled the ignore filter for the whole workspace.
  await symlink(join(outside, 'linked'), join(root, 'linked'));

  const layout = workspaceLayout(root);

  assert.deepEqual(
    layout.packages.map(({directory}) => directory),
    ['linked', 'stub'],
  );
  // `stub` keeps git's answer, so its report and bundle directories stay out. `linked`'s children
  // have no git answer, so they fall back to the build and install names, which do not include
  // `coverage`: that subtree degrades to the documented fallback and nothing else does.
  assert.deepEqual(layout.scanRoots, ['linked/coverage', 'linked/src', 'stub/src']);
});

test('workspace layout reports a package whose own directory git ignores', async t => {
  const root = await workspaceFixture(t, 'vibesys-layout-ignored-member-');
  // `pnpm -r` runs this member's `build`, `check`, and `test` whatever `.gitignore` says, and this
  // repository's `.gitignore` names 30 bare directories, `lib/` among them. Filtering the package
  // question by ignore status dropped such a member from every gate instead of reporting it.
  await writeFile(join(root, '.gitignore'), 'lib/\n');
  execFileSync('git', ['init', '--quiet'], {cwd: root});
  const scripts = {scripts: {build: 'true', check: 'true', test: 'true'}};
  await writePackage(root, 'lib', '@vibesys/lib', ['src'], scripts);
  await writePackage(root, 'normal', '@vibesys/normal', ['src'], scripts);
  await writeFile(join(root, 'lib/src/index.ts'), '');
  await writeFile(join(root, 'normal/src/index.ts'), '');
  await writeFile(
    join(root, 'tsconfig.architecture.json'),
    JSON.stringify({compilerOptions: {paths: {'@vibesys/normal': ['normal/src/index.ts']}}}),
  );

  const layout = workspaceLayout(root);

  assert.deepEqual(
    layout.packages.map(({name, directory}) => ({name, directory})),
    [
      {name: '@vibesys/lib', directory: 'lib'},
      {name: '@vibesys/normal', directory: 'normal'},
    ],
  );
  // Everything inside an ignored package is ignored too, so it contributes no scan root. It is
  // still a package, so the meta-check names it instead of letting it join unchecked.
  assert.deepEqual(layout.scanRoots, ['normal/src']);
  assert.deepEqual(declarationErrors(root, layout), [
    'tsconfig.architecture.json: @vibesys/lib must map to ["lib/src/index.ts"]',
  ]);
});

test('workspace layout drops a symlink it cannot resolve', async t => {
  const root = await workspaceFixture(t, 'vibesys-layout-broken-link-');
  await writePackage(root, 'stub', '@vibesys/stub', ['src']);
  await writeFile(join(root, 'stub/src/index.ts'), '');
  await symlink('absent', join(root, 'dangling'));
  await symlink(join(root, 'stub/src/index.ts'), join(root, 'to-a-file'));
  // A loop is the case `statSync`'s `{throwIfNoEntry: false}` does not cover: it raises ELOOP
  // instead of returning undefined, which aborted every gate importing this module with a raw
  // errno. pnpm cannot resolve such a link either, so dropping it agrees with the member set.
  await symlink('loop-b', join(root, 'loop-a'));
  await symlink('loop-a', join(root, 'loop-b'));

  const layout = workspaceLayout(root);

  assert.deepEqual(
    layout.packages.map(({directory}) => directory),
    ['stub'],
  );
  assert.deepEqual(layout.scanRoots, ['stub/src']);
});

test('workspace layout rejects a packages pattern it cannot expand', async t => {
  const root = await scratchDirectory(t, 'vibesys-layout-pattern-');
  await writeFile(join(root, 'pnpm-workspace.yaml'), 'packages:\n  - "libs/*"\n');

  assert.throws(() => workspaceLayout(root), /unsupported packages pattern "libs\/\*"/);
});

test('declarations accept a package whose entry points and scripts agree with the layout', async t => {
  const root = await workspaceFixture(t, 'vibesys-declarations-');
  await writePackage(root, 'stub', '@vibesys/stub', ['src'], {
    scripts: {build: 'true', check: 'true', test: 'true'},
  });
  await writeFile(join(root, 'stub/src/index.ts'), '');
  await writeFile(
    join(root, 'tsconfig.architecture.json'),
    JSON.stringify({compilerOptions: {paths: {'@vibesys/stub': ['stub/src/index.ts']}}}),
  );

  assert.deepEqual(declarationErrors(root, workspaceLayout(root)), []);
});

test('declarations name a package that joins the workspace unchecked', async t => {
  const root = await workspaceFixture(t, 'vibesys-declarations-stub-');
  await writePackage(root, 'stub', '@vibesys/stub', ['src']);
  await writeFile(join(root, 'tsconfig.architecture.json'), JSON.stringify({compilerOptions: {}}));

  assert.deepEqual(declarationErrors(root, workspaceLayout(root)), [
    'tsconfig.architecture.json: @vibesys/stub must map to ["stub/src/index.ts"]',
    'stub/package.json: @vibesys/stub must declare a "build" script, because `pnpm -r build` ' +
      'skips a package that does not',
    'stub/package.json: @vibesys/stub must declare a "check" script, because `pnpm -r check` ' +
      'skips a package that does not',
    'stub/package.json: @vibesys/stub must declare a "test" script, because `pnpm -r test` ' +
      'skips a package that does not',
  ]);
});

test('declarations reject a path map entry no package exports', async t => {
  const root = await workspaceFixture(t, 'vibesys-declarations-extra-');
  await writePackage(root, 'stub', '@vibesys/stub', ['src'], {
    scripts: {build: 'true', check: 'true', test: 'true'},
    exports: {'.': {import: './dist/index.js'}, './node': {import: './dist/node/index.js'}},
  });
  await writeFile(join(root, 'stub/src/index.ts'), '');
  await mkdir(join(root, 'stub/src/node'), {recursive: true});
  await writeFile(join(root, 'stub/src/node/index.ts'), '');
  await writeFile(
    join(root, 'tsconfig.architecture.json'),
    JSON.stringify({
      compilerOptions: {
        paths: {
          '@vibesys/stub': ['stub/src/index.ts'],
          '@vibesys/stub/node': ['stub/src/node/index.ts'],
          '@vibesys/gone': ['gone/src/index.ts'],
        },
      },
    }),
  );

  assert.deepEqual(declarationErrors(root, workspaceLayout(root)), [
    'tsconfig.architecture.json: @vibesys/gone is not a workspace package export',
  ]);
});

test('path alternation escapes a directory name', () => {
  assert.equal(pathAlternation(['core-state', 'web.next']), 'core-state|web\\.next');
});

// The scan roots are the one thing the gate derives with no second declaration to check it
// against: `cruiseWorkspace` hands them straight to dependency-cruiser, so a root the derivation
// drops is a rule that cannot fire, silently. One probe module per scan root this repository has,
// each importing an undeclared package, and the gate must report every one.
const SCAN_ROOT_PROBES = REPOSITORY_LAYOUT.scanRoots.map(scanRoot => `${scanRoot}/probe.ts`);

test('the architecture gate cruises every directory the layout derives', async t => {
  const {output, exitCode} = await cruiseFixture(
    t,
    Object.fromEntries(SCAN_ROOT_PROBES.map(file => [file, "import 'undeclared-package';\n"])),
  );

  // dependency-cruiser exits with its error-severity violation count, so this pins the total:
  // every probe is reported and nothing else is. It cannot detect a duplicated scan root, because
  // dependency-cruiser deduplicates modules before applying rules, and a repeated root leaves the
  // count unchanged. The direction that matters is a dropped root, which the loop below catches.
  assert.equal(exitCode, SCAN_ROOT_PROBES.length);
  for (const file of SCAN_ROOT_PROBES) {
    assert.ok(
      output.includes(`production-dependencies-are-declared: ${file}`),
      `${file} is not a scan root, so no rule can reach it. Report:\n${output}`,
    );
  }
});

test('the architecture gate reports a workspace whose layering is clean', async t => {
  const {output, exitCode} = await cruiseFixture(t, {
    'tui/src/index.ts': "import '@vibesys/core-state';\n",
    'web/src/index.ts': "import '@vibesys/core-state';\n",
    'core-state/src/index.ts': "import '@vibesys/backend-client';\n",
  });

  assert.equal(exitCode, 0);
  assert.match(output, /no dependency violations found/);
});

/**
 * Runs the real `cruiseWorkspace` over a throwaway workspace. The fixture's own
 * `.dependency-cruiser.mjs` re-exports the repository rule set, so the rules are the real ones
 * while the scan roots come from the fixture's directories.
 */
async function cruiseFixture(t, files) {
  const root = await workspaceFixture(t, 'vibesys-cruise-');
  await writeFile(
    join(root, '.dependency-cruiser.mjs'),
    `export {default} from ${JSON.stringify(pathToFileURL(CONFIG).href)};\n`,
  );
  await writeFile(
    join(root, 'tsconfig.architecture.json'),
    JSON.stringify({
      compilerOptions: {
        baseUrl: '.',
        paths: Object.fromEntries(
          PACKAGE_DIRECTORIES.map(directory => [
            `@vibesys/${directory}`,
            [`${directory}/src/index.ts`],
          ]),
        ),
      },
    }),
  );
  await writeFile(join(root, 'package.json'), JSON.stringify({name: 'fixture-workspace'}));
  await writeExternalPackage(root, 'undeclared-package');
  for (const directory of PACKAGE_DIRECTORIES) {
    await writePackage(root, directory, `@vibesys/${directory}`, ['src']);
    await writeFile(join(root, directory, 'src/index.ts'), '');
  }
  for (const [file, source] of Object.entries(files)) {
    await mkdir(dirname(join(root, file)), {recursive: true});
    await writeFile(join(root, file), source);
  }
  const {output, exitCode} = await cruiseWorkspace(root);
  // dependency-cruiser's `err` reporter colorizes through chalk, which turns itself on when `CI`
  // is set as well as for a TTY. So the same report reads `...declared: web/src/probe.ts` on a
  // developer machine and `...declared: \x1b[1mweb/src/probe.ts\x1b[22m` on CI, and an assertion
  // on the raw string passes locally and fails there. The colors are for the human reading the
  // gate's stdout, not for these assertions, so they are dropped at the one seam that reads it.
  return {output: withoutAnsi(output), exitCode};
}

/**
 * Strip SGR escape sequences, the only kind the reporter emits. The escape byte is built outside
 * the pattern because a regex literal containing it is a lint error, and matching `[<digits>m`
 * without it would silently edit a report that happened to contain that text.
 */
const SGR_SEQUENCE = new RegExp(`${'\u001B'}\\[\\d+(?:;\\d+)*m`, 'gu');

function withoutAnsi(text) {
  return text.replaceAll(SGR_SEQUENCE, '');
}

async function writeSource(root, packageDirectory, file, source) {
  const directory = join(root, packageDirectory, 'src');
  await mkdir(directory, {recursive: true});
  await writeFile(join(directory, file), source);
}

/**
 * A throwaway workspace root, removed when the test that asked for it ends. The creator owns the
 * cleanup, on the failing path too: `node:test` runs `t.after` whether the test passed or threw.
 * `tmpdir()` is NFS on some developer machines, where unlinking a file another process holds open
 * silly-renames it and a later `rmdir` fails, so a leaked fixture is more than clutter.
 */
async function workspaceFixture(t, prefix) {
  const root = await scratchDirectory(t, prefix);
  await writeFile(join(root, 'pnpm-workspace.yaml'), 'packages:\n  - "*"\n');
  return root;
}

/** A throwaway directory that is not a workspace root, removed when the test ends. */
async function scratchDirectory(t, prefix) {
  const root = await mkdtemp(join(tmpdir(), prefix));
  t.after(() => rm(root, {recursive: true, force: true}));
  return root;
}

async function writeManifest(root, directory, name, dependencies, scripts = {}) {
  await writePackage(root, directory, name, [], {dependencies, scripts});
}

async function writePackage(root, directory, name, directories, manifest = {}) {
  const packageDirectory = join(root, directory);
  await mkdir(packageDirectory, {recursive: true});
  await writeFile(join(packageDirectory, 'package.json'), JSON.stringify({name, ...manifest}));
  for (const child of directories) {
    await mkdir(join(packageDirectory, child), {recursive: true});
  }
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
