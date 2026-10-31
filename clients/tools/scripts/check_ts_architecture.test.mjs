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
import {declarationErrors, pathAlternation, workspaceLayout} from './workspace_layout.mjs';

const WORKSPACE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
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

test('dependency-cruiser rejects relative paths into a package, cycles, and unresolvable imports', async t => {
  const root = await scratchDirectory(t, 'vibesys-dependency-rules-');
  await writeFile(
    join(root, 'tsconfig.architecture.json'),
    JSON.stringify({
      compilerOptions: {
        baseUrl: '.',
        paths: {
          '@vibesys/transport': ['libs/transport/src/index.ts'],
          '@vibesys/request-identity': ['libs/request-identity/src/index.ts'],
          '@vibesys/transport/testing': ['libs/transport/src/testing/index.ts'],
          '@vibesys/run-state': ['libs/run-state/src/index.ts'],
          '@vibesys/store/testing': ['libs/store/src/testing/index.ts'],
          '@vibesys/feature-run-page/renderer': ['features/run-page/src/renderer/index.ts'],
        },
      },
    }),
  );
  await writeSource(root, 'libs/transport', 'index.ts', "import '../../run-state/src/index.js';\n");
  await writeSource(
    root,
    'libs/run-state',
    'index.ts',
    "import '../../../apps/desktop-shell/src/index.js';\n",
  );
  await writeSource(
    root,
    'apps/desktop-shell',
    'index.ts',
    "import '@vibesys/run-state';\nimport '@vibesys/run-state/private';\nimport './cycle-a.js';\nimport 'missing-package';\n",
  );
  await writeSource(root, 'apps/desktop-shell', 'cycle-a.ts', "import './cycle-b.js';\n");
  await writeSource(root, 'apps/desktop-shell', 'cycle-b.ts', "import './cycle-a.js';\n");

  const options = await extractDepcruiseOptions(CONFIG);
  const result = await cruise(
    ['libs/transport/src', 'libs/run-state/src', 'apps/desktop-shell/src'],
    {
      ...options,
      baseDir: root,
      tsConfig: {fileName: join(root, 'tsconfig.architecture.json')},
    },
  );
  const violatedRules = new Set(
    result.output.summary.violations.map(violation => violation.rule.name),
  );

  assert.deepEqual(
    violatedRules,
    new Set([
      'workspace-packages-use-public-exports',
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
  'libs/request-identity/src/index.ts': '',
  'libs/transport/src/index.ts': '',
  'libs/transport/src/backoff.test.ts': "import './test-support/expect.js';\n",
  'libs/transport/src/test-support/expect.ts': "import 'node:module';\n",
  'libs/transport/src/testing/index.ts': "export {} from './fake-clock.js';\n",
  'libs/transport/src/testing/fake-clock.ts': '',
  'libs/transport/src/testing/fake-clock.test.ts':
    "import '../test-support/expect.js';\nimport './fake-clock.js';\n",
  'libs/run-state/src/index.ts': "import '@vibesys/transport';\n",
  'apps/desktop-shell/src/index.ts': '',
  'libs/run-state/src/shared-fixtures.test.ts': "import '@vibesys/transport/testing';\n",
  'libs/store/src/index.ts': '',
  'libs/store/src/testing/index.ts': "import './model.js';\n",
  'libs/store/src/testing/model.ts': "import '../index.js';\n",
  // A feature's Node entry (`src/main/`) is not held to its browser parts' rules.
  'features/run-page/src/main/index.ts': "import 'node:fs';\nimport '@vibesys/transport';\n",
  // A package's test support may build on another's, and its tests may use another's Fakes.
  'libs/hosts/src/index.ts': '',
  'libs/ipc/src/index.ts': "import type {} from '@vibesys/hosts';\n",
  // The web app's composition enters a feature through its entries, and only the backend adapter
  // imports transport's runtime values. A feature's renderer drives its pure state; the state
  // names transport's types only.
  'apps/web/src/app/compose.ts':
    "import '@vibesys/feature-run-page/renderer';\nimport '../platform/backend.js';\n",
  'apps/web/src/platform/backend.ts': "import '@vibesys/transport';\n",
  'features/run-page/src/renderer/index.ts':
    "import './effects/replay.js';\nimport './ui/view.js';\n",
  'features/run-page/src/renderer/effects/replay.ts': "import '../../state/replay.js';\n",
  'features/run-page/src/renderer/ui/view.ts': "import '../../state/replay.js';\n",
  'features/run-page/src/state/index.ts': "import './replay.js';\n",
  'features/run-page/src/state/replay.ts':
    "import '@vibesys/run-state';\nimport type {} from '@vibesys/transport';\n",
  'features/run-page/src/state/replay.test.ts':
    "import '@vibesys/store/testing';\nimport '../renderer/index.js';\n",
  // A leaf tool reaching its own directory is the edge `tools-are-leaves` must keep allowing, and
  // the only thing its `$1` back-reference permits.
  'apps/web/e2e/live.spec.ts': "import 'declared-package';\nimport './fixtures.js';\n",
  'apps/web/e2e/fixtures.ts': '',
  'tools/scripts/check.mjs': "import 'node:fs';\nimport 'declared-package';\n",
  // The end-to-end specs may use the shared end-to-end support, which `tools-are-leaves` would
  // otherwise refuse as another tool directory.
  'apps/ui-gallery/e2e/gallery.spec.ts': "import '../../../tools/e2e/support.js';\n",
  'tools/e2e/support.ts': '',
  // The desktop main process: the app composes the packages, the connection manager's shell owns
  // its processes and drives its pure state, and a preload binds `@vibesys/ipc`'s messages to
  // Electron and imports nothing else.
  'apps/desktop-main/src/main/index.ts':
    "import '@vibesys/connection';\nimport '@vibesys/feature-run-page/main';\nimport 'node:path';\n",
  'libs/connection/src/index.ts': "import './shell/link.js';\n",
  'libs/connection/src/shell/link.ts':
    "import 'node:child_process';\nimport '../state/machine.js';\n",
  'libs/connection/src/state/machine.ts': "import type {} from '@vibesys/transport';\n",
  'libs/connection/src/testing/index.ts': "import '../state/machine.js';\n",
  'apps/desktop-main/src/preload/bridge.cts':
    "import 'electron';\nimport type {} from '@vibesys/ipc';\n",
};

const RULE_CASES = [
  {
    rule: 'connection-state-is-pure',
    files: {'libs/connection/src/state/machine.ts': "import '@vibesys/request-identity';\n"},
  },
  {
    rule: 'feature-state-is-pure',
    files: {'features/run-page/src/state/replay.ts': "import '@vibesys/request-identity';\n"},
  },
  {
    rule: 'desktop-preloads-import-only-ipc-and-electron',
    files: {'apps/desktop-main/src/preload/bridge.cts': "import '../main/index.js';\n"},
  },
  {
    rule: 'desktop-preloads-import-only-ipc-and-electron',
    files: {'apps/desktop-main/src/preload/bridge.cts': "import type {} from '@vibesys/hosts';\n"},
  },
  {
    rule: 'desktop-preloads-import-only-ipc-and-electron',
    files: {
      'apps/desktop-main/src/preload/bridge.cts': "import '@vibesys/feature-run-page/renderer';\n",
    },
  },
  {
    rule: 'desktop-preloads-import-only-ipc-and-electron',
    files: {'apps/desktop-main/src/preload/bridge.cts': "import 'node:fs';\n"},
  },
  {
    rule: 'production-code-does-not-import-other-packages-test-support',
    files: {
      'features/run-page/src/renderer/effects/replay.ts': "import '@vibesys/connection/testing';\n",
    },
  },
  {
    rule: 'desktop-main-does-not-import-preloads',
    files: {'apps/desktop-main/src/main/index.ts': "import '../preload/bridge.cjs';\n"},
  },
  {
    rule: 'connection-state-is-pure',
    files: {'libs/connection/src/state/machine.ts': "import '../shell/link.js';\n"},
  },
  {
    rule: 'connection-state-has-no-node-runtime',
    files: {'libs/connection/src/state/machine.ts': "import 'node:fs';\n"},
  },
  {
    rule: 'desktop-only-connection-shell-opens-processes-and-sockets',
    files: {'features/run-page/src/main/index.ts': "import 'node:net';\n"},
  },
  {
    rule: 'desktop-only-connection-shell-opens-processes-and-sockets',
    files: {'apps/desktop-main/src/main/index.ts': "import 'node:child_process';\n"},
  },
  {
    rule: 'renderers-do-not-import-electron',
    files: {
      'apps/desktop-shell/src/shell.ts': "import 'electron';\n",
      'node_modules/electron/package.json': '{"name":"electron","main":"index.js"}',
      'node_modules/electron/index.js': 'export {};\n',
    },
  },
  {
    rule: 'renderers-do-not-import-electron',
    files: {'features/run-page/src/renderer/effects/replay.ts': "import 'electron';\n"},
  },
  {
    rule: 'web-opens-no-sockets-or-processes',
    files: {'apps/desktop-shell/src/shell.ts': "import 'node:child_process';\n"},
  },
  {
    rule: 'web-opens-no-sockets-or-processes',
    files: {'features/run-page/src/renderer/effects/replay.ts': "import 'net';\n"},
  },
  {
    rule: 'production-code-does-not-import-store-test-support',
    files: {'features/run-page/src/state/replay.ts': "import '@vibesys/store/testing';\n"},
  },
  {
    rule: 'production-code-does-not-import-store-test-support',
    files: {
      'apps/desktop-shell/src/shell.ts': "import '@vibesys/store/testing';\n",
    },
  },
  {
    rule: 'feature-state-is-pure',
    files: {'features/run-page/src/state/replay.ts': "import '../renderer/index.js';\n"},
  },
  {
    rule: 'feature-ui-does-not-depend-on-shell',
    files: {'features/run-page/src/renderer/ui/view.ts': "import '../effects/replay.js';\n"},
  },
  {
    rule: 'feature-effects-do-not-import-ui',
    files: {'features/run-page/src/renderer/effects/replay.ts': "import '../ui/view.js';\n"},
  },
  {
    rule: 'feature-transport-is-type-only',
    files: {'features/run-page/src/renderer/effects/replay.ts': "import '@vibesys/transport';\n"},
  },
  {
    rule: 'tools-are-leaves',
    files: {'apps/desktop-shell/src/index.ts': "import '../../../tools/scripts/check.mjs';\n"},
  },
  {
    rule: 'tools-are-leaves',
    files: {
      'apps/ui-gallery/e2e/gallery.spec.ts': "import '../../desktop-shell/e2e/world.js';\n",
      'apps/desktop-shell/e2e/world.ts': '',
    },
  },
  {
    rule: 'shared-e2e-support-is-for-e2e-specs',
    files: {'apps/desktop-shell/src/index.ts': "import '../../../tools/e2e/support.js';\n"},
  },
  {
    rule: 'shared-e2e-support-is-for-e2e-specs',
    files: {'libs/run-state/bench/run-map.ts': "import '../../../tools/e2e/support.js';\n"},
  },
  {
    rule: 'scripts-do-not-import-package-code',
    files: {'tools/scripts/check.mjs': "import '../../apps/desktop-shell/src/index.js';\n"},
  },
  {
    rule: 'production-code-does-not-import-tests',
    files: {'apps/desktop-shell/src/index.ts': "import './index.test.js';\n"},
  },
  {
    rule: 'production-code-does-not-import-transport-test-support',
    files: {'libs/transport/src/index.ts': "import './test-support/expect.js';\n"},
  },
  {
    rule: 'production-code-does-not-import-transport-test-support',
    files: {
      'libs/transport/src/index.ts': "import './testing/fake-clock.js';\n",
    },
  },
  {
    rule: 'production-code-does-not-import-transport-test-support',
    files: {
      'libs/run-state/src/index.ts': "import '@vibesys/transport/testing';\n",
    },
  },
  {
    rule: 'workspace-packages-use-public-exports',
    files: {'apps/desktop-shell/src/index.ts': "import '../../../libs/run-state/src/index.js';\n"},
  },
  {
    rule: 'no-unresolvable-imports',
    files: {'tools/scripts/check.mjs': "import 'missing-package';\n"},
  },
  {
    rule: 'no-circular-dependencies',
    files: {
      'apps/desktop-shell/src/a.ts': "import './b.js';\n",
      'apps/desktop-shell/src/b.ts': "import './a.js';\n",
    },
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
          '@vibesys/transport': ['libs/transport/src/index.ts'],
          '@vibesys/request-identity': ['libs/request-identity/src/index.ts'],
          '@vibesys/transport/testing': ['libs/transport/src/testing/index.ts'],
          '@vibesys/run-state': ['libs/run-state/src/index.ts'],
          '@vibesys/store/testing': ['libs/store/src/testing/index.ts'],
          '@vibesys/feature-run-page/renderer': ['features/run-page/src/renderer/index.ts'],
          '@vibesys/feature-run-page/main': ['features/run-page/src/main/index.ts'],
          '@vibesys/connection': ['libs/connection/src/index.ts'],
          '@vibesys/hosts': ['libs/hosts/src/index.ts'],
          '@vibesys/ipc': ['libs/ipc/src/index.ts'],
          '@vibesys/connection/testing': ['libs/connection/src/testing/index.ts'],
        },
      },
    }),
  );
  await writeExternalPackage(root, 'declared-package');
  await writeExternalPackage(root, 'electron');
  await writeFile(join(root, 'package.json'), JSON.stringify({name: 'fixture-workspace'}));
  for (const directory of PACKAGE_DIRECTORIES) {
    await writePackage(root, directory, `@vibesys/${directory}`, ['src']);
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
      tsConfig: {fileName: tsConfigFile},
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

test('every dependency-cruiser rule has a failing fixture', async () => {
  const options = await extractDepcruiseOptions(CONFIG);
  const covered = new Set(RULE_CASES.map(({rule}) => rule));
  const uncovered = options.ruleSet.forbidden
    .map(({name}) => name)
    .filter(name => !covered.has(name));
  assert.deepEqual(uncovered, []);
});

for (const {rule, files} of RULE_CASES) {
  test(`${rule} rejects ${Object.keys(files)[0]}: ${Object.values(files)[0].trim()}`, async t => {
    assert.ok((await violatedRules(t, {...VALID_FILES, ...files})).includes(rule));
  });
}

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
    await architectureConfigPath(root),
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
  assert.deepEqual(layout.scanRoots, ['normal/src', 'tools']);
  assert.deepEqual(declarationErrors(root, layout), [
    'tools/tsconfig.architecture.json: @vibesys/lib must map to ["lib/src/index.ts"]',
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
  await writeFile(join(root, 'pnpm-workspace.yaml'), 'packages:\n  - "libs/**"\n');

  assert.throws(() => workspaceLayout(root), /unsupported packages pattern "libs\/\*\*"/);
});

test('declarations accept a package whose entry points and scripts agree with the layout', async t => {
  const root = await workspaceFixture(t, 'vibesys-declarations-');
  await writePackage(root, 'stub', '@vibesys/stub', ['src'], {
    scripts: {build: 'true', check: 'true', test: 'true'},
  });
  await writeFile(join(root, 'stub/src/index.ts'), '');
  await writeFile(
    await architectureConfigPath(root),
    JSON.stringify({compilerOptions: {paths: {'@vibesys/stub': ['stub/src/index.ts']}}}),
  );

  assert.deepEqual(declarationErrors(root, workspaceLayout(root)), []);
});

test('declarations need no path map entry for an app that exports nothing', async t => {
  const root = await workspaceFixture(t, 'vibesys-declarations-app-');
  await writePackage(root, 'page', '@vibesys/app-page', ['src'], {
    scripts: {build: 'true', check: 'true', test: 'true'},
    vibesys: {layer: 'app', entries: {'.': 'dom'}},
  });
  await writeFile(await architectureConfigPath(root), JSON.stringify({compilerOptions: {}}));

  assert.deepEqual(declarationErrors(root, workspaceLayout(root)), []);
});

test('declarations name a package that joins the workspace unchecked', async t => {
  const root = await workspaceFixture(t, 'vibesys-declarations-stub-');
  await writePackage(root, 'stub', '@vibesys/stub', ['src']);
  await writeFile(await architectureConfigPath(root), JSON.stringify({compilerOptions: {}}));

  assert.deepEqual(declarationErrors(root, workspaceLayout(root)), [
    'tools/tsconfig.architecture.json: @vibesys/stub must map to ["stub/src/index.ts"]',
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
    exports: {
      '.': {'@vibesys/source': './src/index.ts', default: './dist/index.js'},
      './node': {'@vibesys/source': './src/node/index.ts', default: './dist/node/index.js'},
    },
  });
  await writeFile(join(root, 'stub/src/index.ts'), '');
  await mkdir(join(root, 'stub/src/node'), {recursive: true});
  await writeFile(join(root, 'stub/src/node/index.ts'), '');
  await writeFile(
    await architectureConfigPath(root),
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
    'tools/tsconfig.architecture.json: @vibesys/gone is not a workspace package export',
  ]);
});

test('path alternation escapes a directory name', () => {
  assert.equal(pathAlternation(['run-state', 'web.next']), 'run-state|web\\.next');
});

// The scan roots are the one thing the gate derives with no second declaration to check it
// against: `cruiseWorkspace` hands them straight to dependency-cruiser, so a root the derivation
// drops is a rule that cannot fire, silently. One probe module per scan root this repository has,
// each importing an undeclared package, and the gate must report every one.
const SCAN_ROOT_PROBES = REPOSITORY_LAYOUT.scanRoots.map(scanRoot => `${scanRoot}/probe.ts`);

test('the architecture gate cruises every directory the layout derives', async t => {
  const {output, exitCode} = await cruiseFixture(
    t,
    Object.fromEntries(SCAN_ROOT_PROBES.map(file => [file, "import 'missing-package';\n"])),
  );

  // dependency-cruiser exits with its error-severity violation count, so this pins the total:
  // every probe is reported and nothing else is. It cannot detect a duplicated scan root, because
  // dependency-cruiser deduplicates modules before applying rules, and a repeated root leaves the
  // count unchanged. The direction that matters is a dropped root, which the loop below catches.
  assert.equal(exitCode, SCAN_ROOT_PROBES.length);
  for (const file of SCAN_ROOT_PROBES) {
    assert.ok(
      output.includes(`no-unresolvable-imports: ${file}`),
      `${file} is not a scan root, so no rule can reach it. Report:\n${output}`,
    );
  }
});

test('the architecture gate reports a workspace whose layering is clean', async t => {
  const {output, exitCode} = await cruiseFixture(t, {
    'apps/desktop-shell/src/index.ts': "import '@vibesys/run-state';\n",
    'libs/run-state/src/index.ts': "import '@vibesys/transport';\n",
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
    await architectureConfigPath(root),
    JSON.stringify({
      include: ['../**/*'],
      compilerOptions: {
        baseUrl: '..',
        paths: Object.fromEntries(
          PACKAGE_DIRECTORIES.map(directory => [
            `@vibesys/${directory.split('/').at(-1)}`,
            [`${directory}/src/index.ts`],
          ]),
        ),
      },
    }),
  );
  await writeFile(join(root, 'package.json'), JSON.stringify({name: 'fixture-workspace'}));
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
  // is set as well as for a TTY. So the same report reads `...imports: web/src/probe.ts` on a
  // developer machine and `...imports: \x1b[1mweb/src/probe.ts\x1b[22m` on CI, and an assertion
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
  await writeFile(
    join(root, 'pnpm-workspace.yaml'),
    'packages:\n  - "*"\n  - "libs/*"\n  - "features/*"\n  - "apps/*"\n',
  );
  return root;
}

/** Where the workspace keeps its architecture tsconfig, with the directory made. */
async function architectureConfigPath(root) {
  await mkdir(join(root, 'tools'), {recursive: true});
  return join(root, 'tools', 'tsconfig.architecture.json');
}

/** A throwaway directory that is not a workspace root, removed when the test ends. */
async function scratchDirectory(t, prefix) {
  const root = await mkdtemp(join(tmpdir(), prefix));
  t.after(() => rm(root, {recursive: true, force: true}));
  return root;
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

test('external package export maps resolve public subpaths and reject private ones', async t => {
  const root = await scratchDirectory(t, 'vibesys-export-maps-');
  await writeFile(join(root, 'tsconfig.json'), '{}');
  const packageRoot = join(root, 'node_modules', 'exported-package');
  await mkdir(packageRoot, {recursive: true});
  await writeFile(
    join(packageRoot, 'package.json'),
    JSON.stringify({
      name: 'exported-package',
      type: 'module',
      exports: {'./public': {import: {types: './public.d.ts', default: './public.js'}}},
    }),
  );
  await writeFile(join(packageRoot, 'public.d.ts'), 'export declare const value: number;');
  await writeFile(join(packageRoot, 'public.js'), 'export const value = 1;');
  await writeFile(join(packageRoot, 'private.js'), 'export const secret = 2;');
  await writeSource(
    root,
    'libs/proto',
    'index.ts',
    "import 'exported-package/public';\nimport 'exported-package/private';\n",
  );
  const options = await extractDepcruiseOptions(CONFIG);
  const result = await cruise(['libs/proto/src'], {
    ...options,
    baseDir: root,
    tsConfig: {fileName: join(root, 'tsconfig.json')},
  });
  const missing = result.output.summary.violations
    .filter(violation => violation.rule.name === 'no-unresolvable-imports')
    .map(violation => violation.to);
  assert.deepEqual(missing, ['exported-package/private']);
});
