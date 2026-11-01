import assert from 'node:assert/strict';
import {test} from 'node:test';
import {
  affectedSpecs,
  areaErrors,
  loadWorld,
  MAX_SHARDS,
  SECONDS_PER_SHARD,
  SPEC_BUDGET_SECONDS,
  shardsFor,
  specPackages,
  withinBudget,
} from './e2e_affected.mjs';

const WORLD = loadWorld();
const SHELL = WORLD.specs.filter(spec => spec.startsWith('apps/desktop-shell/'));

const ALL_TOY_SPECS = toyWorld().specs.toSorted();

/** A world of two packages and four specs, small enough to state every answer. */
function toyWorld() {
  const package_ = (name, directory, dependencies = []) => [
    name,
    {name, directory, dependencies, allDependencies: dependencies},
  ];
  return {
    specs: [
      'apps/shell/e2e/a.spec.ts',
      'apps/shell/e2e/b.spec.ts',
      'apps/shell/e2e/c.spec.ts',
      'apps/other/e2e/d.spec.ts',
    ],
    seconds: {},
    imports: new Map([
      ['apps/shell/e2e/a.spec.ts', {files: ['apps/shell/e2e/world.ts'], packages: []}],
      ['apps/shell/e2e/b.spec.ts', {files: ['apps/shell/e2e/world.ts'], packages: ['@t/lib']}],
      ['apps/shell/e2e/c.spec.ts', {files: [], packages: []}],
      ['apps/shell/e2e/world.ts', {files: ['apps/shell/e2e/deep.ts'], packages: []}],
      ['apps/shell/e2e/deep.ts', {files: [], packages: []}],
      ['apps/other/e2e/d.spec.ts', {files: [], packages: []}],
    ]),
    packages: new Map([
      package_('@t/feature', 'features/feature', ['@t/lib']),
      package_('@t/lib', 'libs/lib'),
      package_('@t/unrelated', 'libs/unrelated'),
      package_('@t/shell', 'apps/shell', ['@t/feature', '@t/unrelated']),
      package_('@t/other', 'apps/other', ['@t/lib']),
    ]),
    areas: {
      frame: [],
      apps: {shell: {feature: {specs: ['a.spec.ts', 'b.spec.ts'], packages: ['@t/feature']}}},
    },
  };
}

const selected = (paths, world = toyWorld()) => affectedSpecs(paths, world).specs;

test('a spec selects itself and nothing else', () => {
  assert.deepEqual(selected(['clients/apps/shell/e2e/c.spec.ts']), ['apps/shell/e2e/c.spec.ts']);
});

test('a helper selects the specs that import it, however deep', () => {
  assert.deepEqual(selected(['clients/apps/shell/e2e/deep.ts']), [
    'apps/shell/e2e/a.spec.ts',
    'apps/shell/e2e/b.spec.ts',
  ]);
});

test('a file under e2e that no spec imports selects every spec of its app', () => {
  assert.deepEqual(selected(['clients/apps/shell/e2e/scenarios/data.json']), [
    'apps/shell/e2e/a.spec.ts',
    'apps/shell/e2e/b.spec.ts',
    'apps/shell/e2e/c.spec.ts',
  ]);
});

test('a package selects the specs of the areas that reach it, and those that import it', () => {
  // `lib` is reached through the feature by a and b; c is placed in no area, so it depends on
  // everything its app does (the feature, unrelated, and so lib); d's app depends on lib.
  assert.deepEqual(selected(['clients/libs/lib/src/x.ts']), ALL_TOY_SPECS);
  assert.deepEqual(selected(['clients/features/feature/src/x.ts']), [
    'apps/shell/e2e/a.spec.ts',
    'apps/shell/e2e/b.spec.ts',
    'apps/shell/e2e/c.spec.ts',
  ]);
});

test('a package no spec declares selects every spec of an app that depends on it', () => {
  const world = toyWorld();
  world.specs.pop();
  world.imports.delete('apps/other/e2e/d.spec.ts');
  world.areas.apps.shell.feature.specs = ['a.spec.ts', 'b.spec.ts', 'c.spec.ts'];
  // `unrelated` is in the shell's dependencies but in no area's closure and no spec's imports.
  assert.deepEqual(selected(['clients/libs/unrelated/src/x.ts'], world), world.specs);
});

test('an app file selects every spec of the app', () => {
  assert.deepEqual(selected(['clients/apps/shell/src/main.tsx']), [
    'apps/shell/e2e/a.spec.ts',
    'apps/shell/e2e/b.spec.ts',
    'apps/shell/e2e/c.spec.ts',
  ]);
});

test('a path outside clients selects nothing, an unknown path under it everything', () => {
  assert.deepEqual(selected(['src/vibesys/run/host.py', '.github/workflows/test.yml']), []);
  assert.deepEqual(selected(['clients/new-root-file.cfg']), ALL_TOY_SPECS);
});

test('lint configuration, unit tests, documents and recorded seconds select nothing', () => {
  assert.deepEqual(
    selected([
      'clients/biome.json',
      'clients/tools/e2e/durations.json',
      'clients/tools/lint-rules/rule.grit',
      'clients/libs/lib/src/x.test.ts',
      'clients/libs/lib/src/x.property.dom.test.tsx',
      'clients/libs/lib/node-tests/y.ts',
      'clients/libs/lib/README.md',
    ]),
    [],
  );
});

test('selection is the union of the selections of each changed path', () => {
  const paths = [
    'clients/apps/shell/e2e/c.spec.ts',
    'clients/apps/shell/e2e/deep.ts',
    'clients/libs/lib/src/x.ts',
    'clients/apps/other/src/y.ts',
    'src/a.py',
  ];
  for (let mask = 0; mask < 1 << paths.length; mask += 1) {
    const subset = paths.filter((_, index) => (mask >> index) & 1);
    const expected = new Set(subset.flatMap(path => selected([path])));
    assert.deepEqual(selected(subset), [...expected].sort(), subset.join(' '));
  }
});

test('the repository is classified: no area errors, and every spec has a package closure', () => {
  assert.deepEqual(areaErrors(WORLD), []);
  const closures = specPackages(WORLD);
  for (const spec of WORLD.specs)
    assert.ok(closures.get(spec)?.size, `${spec} depends on no package`);
});

test('every spec is selected by a change to itself and by one to each package it depends on', () => {
  const closures = specPackages(WORLD);
  for (const spec of WORLD.specs) {
    assert.ok(affectedSpecs([`clients/${spec}`], WORLD).specs.includes(spec), spec);
    for (const name of closures.get(spec)) {
      const directory = WORLD.packages.get(name).directory;
      const specs = affectedSpecs([`clients/${directory}/src/changed.ts`], WORLD).specs;
      assert.ok(specs.includes(spec), `${spec} is not selected by a change to ${name}`);
    }
  }
});

test('a change to any dependency of an app selects at least one spec of that app', () => {
  for (const app of WORLD.packages.values()) {
    if (!app.directory.startsWith('apps/')) continue;
    const name = app.directory.slice('apps/'.length);
    if (!WORLD.specs.some(spec => spec.startsWith(`apps/${name}/`))) continue;
    for (const dependency of app.allDependencies) {
      const directory = WORLD.packages.get(dependency).directory;
      const specs = affectedSpecs([`clients/${directory}/src/changed.ts`], WORLD).specs;
      assert.ok(
        specs.some(spec => spec.startsWith(`apps/${name}/`)),
        `${dependency} reaches no spec of ${name}`,
      );
    }
  }
});

test('a change to a feature package selects its own specs and not the other features', () => {
  const specs = affectedSpecs(['clients/features/usage/src/changed.ts'], WORLD).specs;
  assert.ok(specs.includes('apps/desktop-shell/e2e/usage.spec.ts'));
  assert.ok(!specs.includes('apps/desktop-shell/e2e/machines.spec.ts'));
  assert.ok(!specs.includes('apps/desktop-shell/e2e/new-run.spec.ts'));
  assert.ok(specs.length < SHELL.length / 2);
});

// #3583 changed the screens' `data-state` and the e2e helpers. The pull request stage skipped
// the end-to-end job and the queue failed these three specs, then the two entries behind it.
test('the change of #3583 selects the specs the queue failed on', () => {
  const specs = affectedSpecs(
    [
      'clients/apps/desktop-shell/e2e/read.ts',
      'clients/apps/desktop-shell/e2e/world.ts',
      'clients/apps/desktop-shell/src/shell.tsx',
      'clients/features/run-page/src/run-page.tsx',
    ],
    WORLD,
  ).specs;
  for (const failed of ['run-intents', 'run-load', 'run-history-loading']) {
    assert.ok(specs.includes(`apps/desktop-shell/e2e/${failed}.spec.ts`), failed);
  }
});

test('the jobs fit the recorded seconds: none for no specs, one per slice, never above the queue', () => {
  assert.equal(shardsFor([], {}), 0);
  assert.equal(shardsFor(['a'], {a: 1}), 1);
  assert.equal(shardsFor(['a'], {a: SECONDS_PER_SHARD + 1}), 2);
  assert.equal(shardsFor(['a', 'b'], {a: 1e6, b: 1e6}), MAX_SHARDS);
  let previous = 0;
  for (let count = 0; count <= 12; count += 1) {
    const specs = Array.from({length: count}, (_, index) => `s${index}`);
    const jobs = shardsFor(specs, Object.fromEntries(specs.map(spec => [spec, 70])));
    assert.ok(jobs >= previous && jobs <= MAX_SHARDS, `${count} specs: ${jobs} jobs`);
    previous = jobs;
  }
});

test('the whole suite across the queue jobs fits the recorded seconds', () => {
  assert.equal(shardsFor(WORLD.specs, WORLD.seconds), MAX_SHARDS);
});

const total = (specs, recorded) => specs.reduce((sum, spec) => sum + (recorded[spec] ?? 0), 0);

test('a pull request always runs a changed spec, over budget if need be', () => {
  const world = toyWorld();
  const recorded = Object.fromEntries(world.specs.map(spec => [spec, 1000]));
  const changed = affectedSpecs(['clients/apps/shell/e2e/c.spec.ts'], world);
  assert.deepEqual(withinBudget(changed, recorded, 10).run, ['apps/shell/e2e/c.spec.ts']);
});

test('the specs of a changed helper run before those of a changed package, fastest first', () => {
  const world = toyWorld();
  const recorded = {
    'apps/shell/e2e/a.spec.ts': 90,
    'apps/shell/e2e/b.spec.ts': 30,
    'apps/shell/e2e/c.spec.ts': 10,
    'apps/other/e2e/d.spec.ts': 10,
  };
  const selection = affectedSpecs(
    ['clients/apps/shell/e2e/deep.ts', 'clients/libs/lib/src/x.ts'],
    world,
  );
  // a and b import the helper (rank 1); lib reaches d with no steps (rank 3: d's app names it) and
  // c through the feature (rank 4: c is in no area, so it names its app's packages).
  const bounded = withinBudget(selection, recorded, 40);
  assert.deepEqual(bounded.run, ['apps/other/e2e/d.spec.ts', 'apps/shell/e2e/b.spec.ts']);
  assert.deepEqual(bounded.deferred, ['apps/shell/e2e/a.spec.ts', 'apps/shell/e2e/c.spec.ts']);
});

test('a package change runs the closest specs first and leaves the rest to the queue', () => {
  const world = toyWorld();
  // lib is named by b's import (0 steps), by a's and c's area through the feature (1 step), and by
  // d's app (not an area: its direct packages are the app's, which include lib).
  const selection = affectedSpecs(['clients/libs/lib/src/x.ts'], world);
  const recorded = {
    'apps/shell/e2e/a.spec.ts': 100,
    'apps/shell/e2e/b.spec.ts': 100,
    'apps/shell/e2e/c.spec.ts': 100,
    'apps/other/e2e/d.spec.ts': 100,
  };
  const bounded = withinBudget(selection, recorded, 250);
  assert.equal(total(bounded.run, recorded), 200);
  assert.ok(bounded.run.includes('apps/shell/e2e/b.spec.ts'), 'the spec that imports lib runs');
  assert.deepEqual([...bounded.run, ...bounded.deferred].sort(), selection.specs);
});

test('what a pull request runs of a package change fits the budget, whatever the package', () => {
  for (const pkg of WORLD.packages.values()) {
    if (pkg.directory.startsWith('apps/')) continue;
    const selection = affectedSpecs([`clients/${pkg.directory}/src/changed.ts`], WORLD);
    const bounded = withinBudget(selection, WORLD.seconds);
    const changedSpecs = bounded.run.filter(spec => selection.ranks.get(spec) === 0);
    const others = bounded.run.filter(spec => selection.ranks.get(spec) !== 0);
    const room = Math.max(0, SPEC_BUDGET_SECONDS - total(changedSpecs, WORLD.seconds));
    assert.ok(total(others, WORLD.seconds) <= room, `${pkg.name} runs more than its budget`);
    assert.deepEqual([...bounded.run, ...bounded.deferred].sort(), selection.specs, pkg.name);
    assert.ok(shardsFor(bounded.run, WORLD.seconds) <= MAX_SHARDS, pkg.name);
  }
});
