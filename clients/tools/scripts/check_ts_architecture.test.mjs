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
          '@vibesys/display-names': ['libs/display-names/src/index.ts'],
          '@vibesys/ui': ['libs/ui/src/index.ts'],
        },
      },
    }),
  );
  await writeSource(root, 'libs/display-names', 'index.ts', "import '../../ui/src/index.js';\n");
  await writeSource(
    root,
    'libs/ui',
    'index.ts',
    "import '../../../apps/ui-gallery/src/index.js';\n",
  );
  await writeSource(
    root,
    'apps/ui-gallery',
    'index.ts',
    "import '@vibesys/ui';\nimport '@vibesys/ui/private';\nimport './cycle-a.js';\nimport 'missing-package';\n",
  );
  await writeSource(root, 'apps/ui-gallery', 'cycle-a.ts', "import './cycle-b.js';\n");
  await writeSource(root, 'apps/ui-gallery', 'cycle-b.ts', "import './cycle-a.js';\n");

  const options = await extractDepcruiseOptions(CONFIG);
  const result = await cruise(['libs/display-names/src', 'libs/ui/src', 'apps/ui-gallery/src'], {
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
  'libs/display-names/src/index.ts': '',
  'libs/ui/src/index.ts': '',
  'apps/ui-gallery/src/index.ts': "import '@vibesys/display-names';\nimport '@vibesys/ui';\n",
  'libs/orchestrator-proto/src/index.ts': '',
  // A leaf tool reaching its own directory is the edge `tools-are-leaves` must keep allowing, and
  // the only thing its `$1` back-reference permits.
  'apps/web/e2e/live.spec.ts': "import 'declared-package';\nimport './fixtures.js';\n",
  'apps/web/e2e/fixtures.ts': '',
  'tools/scripts/check.mjs': "import 'node:fs';\nimport 'declared-package';\n",
  // The end-to-end specs may use the shared end-to-end support, which `tools-are-leaves` would
  // otherwise refuse as another tool directory.
  'apps/ui-gallery/e2e/gallery.spec.ts': "import '../../../tools/e2e/support.js';\n",
  'tools/e2e/support.ts': '',
  // The Effect desktop app: state is plain and stands alone (a type may name another module),
  // core builds on state and Effect, ui on both, and the main process and the preload never see
  // ui. The testing doubles are for tests.
  'apps/desktop/src/state/index.ts':
    "import './fold.js';\nimport type {} from '@vibesys/orchestrator-proto';\n",
  // A fold reads the generated protocol's enums at run time; nothing else outside the folder.
  'apps/desktop/src/state/fold.ts': "import '@vibesys/orchestrator-proto';\n",
  'apps/desktop/src/core/index.ts': "import 'effect/Effect';\nimport '../state/index.js';\n",
  'apps/desktop/src/ui/index.ts': "import '../core/index.js';\nimport '../state/index.js';\n",
  'apps/desktop/src/main/index.ts': "import '../core/index.js';\nimport 'node:path';\n",
  'apps/desktop/src/main/process.ts': "import 'node:child_process';\n",
  'apps/desktop/src/main/local-host.ts': "import 'node:net';\n",
  'apps/desktop/src/preload/index.cts':
    "import 'electron';\nimport type {} from '../core/index.js';\n",
  // Features: the shell composes them, and one does not import another.
  'apps/desktop/src/ui/settings/words.ts': 'export {};\n',
  'apps/desktop/src/ui/shell/shell.ts': "import '../settings/words.js';\n",
  'apps/desktop/src/testing/index.ts': "import '../core/index.js';\n",
  'apps/desktop/src/core/index.test.ts':
    "import '../testing/index.js';\nimport '@effect/vitest';\n",
};

const RULE_CASES = [
  {
    rule: 'desktop-main-opens-sockets-only-in-local-host-ts',
    files: {'apps/desktop/src/main/index.ts': "import 'node:net';\n"},
  },
  {
    rule: 'desktop-main-starts-processes-only-in-process-ts',
    files: {'apps/desktop/src/main/index.ts': "import 'node:child_process';\n"},
  },
  {
    rule: 'desktop-state-imports-nothing-outside-itself',
    files: {'apps/desktop/src/state/fold.ts': "import '../core/index.js';\n"},
  },
  {
    rule: 'desktop-state-imports-nothing-outside-itself',
    files: {'apps/desktop/src/state/fold.ts': "import '@vibesys/ui';\n"},
  },
  {
    rule: 'desktop-state-imports-nothing-outside-itself',
    files: {'apps/desktop/src/state/fold.ts': "import 'node:fs';\n"},
  },
  {
    rule: 'desktop-state-never-imports-effect',
    files: {'apps/desktop/src/state/fold.ts': "import 'effect/Effect';\n"},
  },
  {
    rule: 'desktop-state-never-imports-effect',
    files: {'apps/desktop/src/state/fold.ts': "import type {} from 'effect/Option';\n"},
  },
  {
    rule: 'desktop-features-do-not-import-each-other',
    files: {
      'apps/desktop/src/ui/orchestrator/words.ts': 'export {};\n',
      'apps/desktop/src/ui/settings/words.ts': "import '../orchestrator/words.js';\n",
    },
  },
  {
    rule: 'desktop-features-do-not-import-each-other',
    files: {
      'apps/desktop/src/ui/shell/shell.ts': 'export {};\n',
      'apps/desktop/src/ui/settings/words.ts': "import '../shell/shell.js';\n",
    },
  },
  {
    rule: 'desktop-core-does-not-reach-the-shells',
    files: {'apps/desktop/src/core/index.ts': "import '../ui/index.js';\n"},
  },
  {
    rule: 'desktop-core-does-not-reach-the-shells',
    files: {'apps/desktop/src/core/index.ts': "import '../main/index.js';\n"},
  },
  {
    rule: 'desktop-ui-does-not-reach-main-or-preload',
    files: {'apps/desktop/src/ui/index.ts': "import '../main/index.js';\n"},
  },
  {
    rule: 'desktop-ui-does-not-reach-main-or-preload',
    files: {'apps/desktop/src/ui/index.ts': "import '../preload/index.cjs';\n"},
  },
  {
    rule: 'desktop-main-and-preload-do-not-import-ui',
    files: {'apps/desktop/src/main/index.ts': "import '../ui/index.js';\n"},
  },
  {
    rule: 'desktop-main-and-preload-do-not-import-ui',
    files: {'apps/desktop/src/preload/index.cts': "import '../ui/index.js';\n"},
  },
  {
    rule: 'desktop-main-does-not-import-the-preload',
    files: {'apps/desktop/src/main/index.ts': "import '../preload/index.cjs';\n"},
  },
  {
    rule: 'desktop-preload-loads-only-electron',
    files: {'apps/desktop/src/preload/index.cts': "import '../core/index.js';\n"},
  },
  {
    rule: 'desktop-preload-loads-only-electron',
    files: {'apps/desktop/src/preload/index.cts': "import 'node:fs';\n"},
  },
  {
    rule: 'desktop-production-code-does-not-import-testing',
    files: {'apps/desktop/src/core/index.ts': "import '../testing/index.js';\n"},
  },
  {
    rule: 'tools-are-leaves',
    files: {'apps/ui-gallery/src/index.ts': "import '../../../tools/scripts/check.mjs';\n"},
  },
  {
    rule: 'tools-are-leaves',
    files: {
      'apps/ui-gallery/e2e/gallery.spec.ts': "import '../../desktop/scripts/world.js';\n",
      'apps/desktop/scripts/world.ts': '',
    },
  },
  {
    rule: 'shared-e2e-support-is-for-e2e-specs',
    files: {'apps/ui-gallery/src/index.ts': "import '../../../tools/e2e/support.js';\n"},
  },
  {
    rule: 'shared-e2e-support-is-for-e2e-specs',
    files: {'libs/ui/bench/run-map.ts': "import '../../../tools/e2e/support.js';\n"},
  },
  {
    rule: 'scripts-do-not-import-package-code',
    files: {'tools/scripts/check.mjs': "import '../../apps/ui-gallery/src/index.js';\n"},
  },
  {
    rule: 'production-code-does-not-import-other-packages-test-support',
    files: {
      'libs/ui/src/testing/index.ts': '',
      'apps/ui-gallery/src/index.ts': "import '../../../libs/ui/src/testing/index.js';\n",
    },
  },
  {
    rule: 'production-code-does-not-import-tests',
    files: {'apps/ui-gallery/src/index.ts': "import './index.test.js';\n"},
  },
  {
    rule: 'workspace-packages-use-public-exports',
    files: {'apps/ui-gallery/src/index.ts': "import '../../../libs/ui/src/index.js';\n"},
  },
  {
    rule: 'no-unresolvable-imports',
    files: {'tools/scripts/check.mjs': "import 'missing-package';\n"},
  },
  {
    rule: 'no-circular-dependencies',
    files: {
      'apps/ui-gallery/src/a.ts': "import './b.js';\n",
      'apps/ui-gallery/src/b.ts': "import './a.js';\n",
    },
  },
  {
    rule: 'desktop-page-code-does-not-import-electron',
    files: {
      'apps/desktop/src/ui/index.ts': "import 'electron';\n",
      'node_modules/electron/package.json': '{"name":"electron","main":"index.js"}',
      'node_modules/electron/index.js': 'export {};\n',
    },
  },
  {
    rule: 'desktop-page-code-opens-no-sockets-or-processes',
    files: {'apps/desktop/src/core/index.ts': "import 'node:child_process';\n"},
  },
  {
    rule: 'desktop-page-code-opens-no-sockets-or-processes',
    files: {'apps/desktop/src/ui/index.ts': "import 'net';\n"},
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
          '@vibesys/display-names': ['libs/display-names/src/index.ts'],
          '@vibesys/orchestrator-proto': ['libs/orchestrator-proto/src/index.ts'],
          '@vibesys/ui': ['libs/ui/src/index.ts'],
        },
      },
    }),
  );
  await writeExternalPackage(root, 'declared-package');
  await writeExternalPackage(root, 'electron');
  await writeExternalPackage(root, 'effect/Effect');
  await writeExternalPackage(root, 'effect/Option');
  await writeExternalPackage(root, '@effect/vitest');
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

test('the git answer comes from the tree, not from the repository a git hook names', async t => {
  // The workspace is a subdirectory of its repository, as `clients/` is, and the repository's
  // `.gitignore` sits above it.
  const repository = await scratchDirectory(t, 'vibesys-layout-hook-env-');
  const root = join(repository, 'clients');
  await mkdir(root);
  await writeFile(
    join(root, 'pnpm-workspace.yaml'),
    'packages:\n  - "*"\n  - "libs/*"\n  - "features/*"\n  - "apps/*"\n',
  );
  await writeFile(join(repository, '.gitignore'), 'coverage/\n');
  execFileSync('git', ['init', '--quiet'], {cwd: repository});
  await writePackage(root, 'stub', '@vibesys/stub', ['src', 'coverage']);
  const expected = workspaceLayout(root).scanRoots;
  assert.deepEqual(expected, ['stub/src']);

  // A pre-push hook runs with `GIT_DIR` (and `GIT_PREFIX`, `GIT_INDEX_FILE`) set for the
  // repository being pushed. Inherited, `GIT_DIR` makes git take the working directory, here
  // `clients/`, as the top of the tree, so the `.gitignore` above it is never read and
  // `check-ignore` fails or answers wrongly. The real hook run then scanned `node_modules` and
  // `dist`: the gate passed in a shell and failed in the hook.
  const hookEnvironment = {GIT_DIR: join(repository, '.git'), GIT_PREFIX: 'clients/'};
  const before = {};
  for (const [name, value] of Object.entries(hookEnvironment)) {
    before[name] = process.env[name];
    process.env[name] = value;
  }
  t.after(() => {
    for (const [name, value] of Object.entries(before)) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  });

  assert.deepEqual(workspaceLayout(root).scanRoots, expected);
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
  assert.equal(pathAlternation(['runs', 'web.next']), 'runs|web\\.next');
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
    'apps/ui-gallery/src/index.ts': "import '@vibesys/display-names';\n",
    'libs/display-names/src/index.ts': "import '@vibesys/ui';\n",
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
