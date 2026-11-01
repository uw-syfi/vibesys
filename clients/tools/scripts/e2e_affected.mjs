#!/usr/bin/env node

/**
 * Which desktop end-to-end specs a change can affect.
 *
 * The pull request stage used to skip `clients-e2e` altogether, so a change that broke a spec was
 * found by the merge queue, which then ejected the change and the entries queued behind it. This
 * module answers "which specs can this change affect" from facts the repository already holds, so
 * the pull request stage runs those and no others.
 *
 * The rule, over the paths a change touches (all relative to `clients/`):
 *
 * - A spec file selects itself. Another file under an app's `e2e/` selects every spec that
 *   imports it, directly or through other files (the import graph of `e2e/` and `tools/e2e/`),
 *   and every spec of the app when no spec imports it (a file read at run time, a screenshot).
 * - A file of a workspace package selects every spec that depends on the package: through the
 *   package dependencies of the app's declared area, through the packages the spec's own imports
 *   reach, and through the packages those depend on. An app's own files select all of its specs.
 * - A package the app depends on that no spec depends on selects every spec of the app: an
 *   undeclared relation is never read as "no effect".
 * - A spec the area table (`tools/e2e/areas.json`) does not place depends on every package the app
 *   depends on. Placing a spec narrows its selection and is the one declaration a person writes.
 * - Anything else under `clients/` that is not known to be test-only (lint and check
 *   configuration, unit tests, documents) selects every spec. A path outside `clients/` selects
 *   none: no Python or workflow change is bundled into the app.
 *
 * `withinBudget` then bounds what a pull request runs. Direct evidence (a changed spec or helper, a
 * changed app file, a file the rule cannot place) always runs. A package change reaches specs by
 * dependencies, and a shared package (`proto`, `contracts`, `ui`) reaches nearly all of them, which
 * would take the whole suite and more than the stage's budget allows. Those specs run closest
 * first (fewest dependency steps between the spec's own packages and the changed one, then
 * shortest) up to `PACKAGE_SPEC_BUDGET_SECONDS`; the rest are named in the log and left to the
 * merge queue, which runs every spec.
 *
 * `shardsFor` sizes the job from the recorded seconds of the specs it runs, never above the
 * queue's `MAX_SHARDS`, which is also the queue's matrix.
 */

import {execFileSync} from 'node:child_process';
import {appendFileSync, existsSync, readdirSync, readFileSync} from 'node:fs';
import {dirname, join, posix, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {e2eSpecs} from '../e2e/shard.mts';
import {workspaceLayout} from './workspace_layout.mjs';

const CLIENTS_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const CLIENTS_PREFIX = 'clients/';

/** The queue runs the whole suite across this many jobs; a pull request never uses more. */
export const MAX_SHARDS = 5;
/** A job should hold about this many recorded seconds of specs before another job is worth its setup. */
export const SECONDS_PER_SHARD = 140;
/**
 * What a pull request's jobs run of the specs a package change reaches only through dependencies:
 * the recorded seconds that fit the jobs, so the stage stays near its budget. The queue runs the rest.
 */
export const PACKAGE_SPEC_BUDGET_SECONDS = MAX_SHARDS * SECONDS_PER_SHARD;

const SOURCE_FILE = /\.(?:[cm]?ts|tsx|mjs|js)$/;
const SOURCE_EXTENSIONS = ['.ts', '.tsx', '.mts', '.cts', '.js', '.mjs', '.json'];
const IMPORT_SPECIFIER =
  /(?:\bfrom\s*|\bimport\s*\(\s*|\bimport\s+)(['"])([^'"\n]+)\1|\brequire\(\s*(['"])([^'"\n]+)\3\s*\)/g;
const ASSET_DIRECTORY = /['"`]\.\/([A-Za-z0-9_-]+)\//g;
const PACKAGE_NAME = /^(@[^/]+\/[^/]+|[^./][^/]*)/;

/** Files that cannot change what the built app does or what a spec reads, by path. */
const TEST_ONLY_PATH = [
  /^tools\/e2e\/durations\.json$/,
  /^biome(?:\.warnings)?\.json$/,
  /^tools\/(?:lint-rules|knip\.config\.ts|tsconfig\.architecture\.json)/,
  /^tools\/scripts\/(?!with_derived_replays|e2e_)/,
  /\.md$/,
  /(?:^|\/)[^/]*\.(?:test|property\.dom\.test)\.[cm]?[jt]sx?$/,
  /(?:^|\/)node-tests\//,
  /^\.dependency-cruiser\.mjs$/,
];

/**
 * @typedef {{name: string, directory: string, dependencies: string[], allDependencies: string[]}} PackageFacts
 * @typedef {{
 *   specs: string[],
 *   seconds: Record<string, number>,
 *   imports: Map<string, {files: string[], packages: string[]}>,
 *   packages: Map<string, PackageFacts>,
 *   areas: {frame: string[], apps: Record<string, Record<string, {specs: string[], packages: string[]}>>},
 * }} E2eWorld
 */

/**
 * Whether `spec` (`desktop-shell/e2e/x.spec.ts`) is matched by one glob of an area, where `*`
 * stands for any run of characters and the glob names a file directly under the app's `e2e/`.
 *
 * @param {string} spec
 * @param {string} glob
 */
function matchesGlob(spec, glob) {
  const file = spec.slice(spec.indexOf('/e2e/') + '/e2e/'.length);
  const pattern = glob.replaceAll(/[\\^$.+?()[\]{}|]/g, '\\$&').replaceAll('*', '.*');
  return new RegExp(`^${pattern}$`).test(file);
}

/**
 * Reads the repository's facts: the specs and their recorded seconds, the import graph of the e2e
 * directories, the workspace packages with their dependencies, and the area table.
 *
 * @param {string} [root] the clients root
 * @returns {E2eWorld}
 */
export function loadWorld(root = CLIENTS_ROOT) {
  const specs = e2eSpecs().map(spec => `apps/${spec}`);
  const seconds = Object.fromEntries(
    Object.entries(readJson(join(root, 'tools/e2e/durations.json'))).map(([spec, value]) => [
      `apps/${spec}`,
      Number(value),
    ]),
  );
  const layout = workspaceLayout(root);
  const names = new Set(layout.packages.map(({name}) => name));
  const packages = new Map(
    layout.packages.map(({name, directory, manifest}) => {
      const keep = fields =>
        fields.flatMap(field => Object.keys(manifest[field] ?? {})).filter(dep => names.has(dep));
      return [
        name,
        {
          name,
          directory,
          dependencies: keep(['dependencies', 'optionalDependencies']),
          allDependencies: keep(['dependencies', 'optionalDependencies', 'devDependencies']),
        },
      ];
    }),
  );
  const areas = readJson(join(root, 'tools/e2e/areas.json'));
  return {
    specs,
    seconds,
    imports: importGraph(root, names),
    packages,
    areas: {
      frame: areas.frame ?? [],
      apps: Object.fromEntries(Object.entries(areas).filter(([key]) => key !== 'frame')),
    },
  };
}

/**
 * The files and packages each source file under an app's `e2e/` or `tools/e2e/` imports.
 *
 * @param {string} root
 * @param {Set<string>} packageNames
 * @returns {Map<string, {files: string[], packages: string[]}>}
 */
function importGraph(root, packageNames) {
  const files = [
    ...readdirSync(join(root, 'apps'), {withFileTypes: true})
      .filter(entry => entry.isDirectory())
      .flatMap(app => sourceFiles(root, `apps/${app.name}/e2e`)),
    ...sourceFiles(root, 'tools/e2e'),
  ];
  return new Map(files.map(file => [file, importsOf(root, file, packageNames)]));
}

/**
 * The files and workspace packages one source file imports.
 *
 * @param {string} root
 * @param {string} file
 * @param {Set<string>} packageNames
 */
function importsOf(root, file, packageNames) {
  const text = readFileSync(join(root, file), 'utf8');
  const specifiers = [...text.matchAll(IMPORT_SPECIFIER)].map(match => match[2] ?? match[4]);
  const relative = specifiers.filter(specifier => specifier.startsWith('.'));
  const files = relative.map(specifier =>
    resolveImport(root, posix.join(posix.dirname(file), specifier)),
  );
  const packages = specifiers
    .filter(specifier => !specifier.startsWith('.'))
    .map(specifier => PACKAGE_NAME.exec(specifier)?.[1])
    .filter(name => name !== undefined && packageNames.has(name));
  // A directory a file names in a path it reads at run time (`./scenarios/${name}.json`).
  const assets = [...text.matchAll(ASSET_DIRECTORY)]
    .map(match => posix.join(posix.dirname(file), match[1]))
    .filter(directory => isDirectory(root, directory));
  return {
    files: [...new Set(files.filter(target => target !== undefined))],
    packages: [...new Set(packages)],
    assets: [...new Set(assets)],
  };
}

/** @param {string} root @param {string} directory */
function sourceFiles(root, directory) {
  if (!existsSync(join(root, directory))) return [];
  return readdirSync(join(root, directory), {withFileTypes: true}).flatMap(entry => {
    const path = `${directory}/${entry.name}`;
    if (entry.isDirectory()) {
      return entry.name === 'node_modules' || entry.name.startsWith('.')
        ? []
        : sourceFiles(root, path);
    }
    return SOURCE_FILE.test(entry.name) ? [path] : [];
  });
}

/**
 * The file a relative import names, or `undefined` for one outside the repository's sources. A
 * `.js` specifier stands for the TypeScript file beside it, as the build resolves it.
 *
 * @param {string} root
 * @param {string} base a path relative to `root` without its resolved extension
 */
function resolveImport(root, base) {
  const stem = base.replace(/\.(?:[cm]?js)$/, '');
  const candidates = [
    base,
    ...SOURCE_EXTENSIONS.map(extension => `${stem}${extension}`),
    ...SOURCE_EXTENSIONS.map(extension => `${base}/index${extension}`),
  ];
  return candidates.find(
    candidate => existsSync(join(root, candidate)) && !isDirectory(root, candidate),
  );
}

/** @param {string} root @param {string} path */
function isDirectory(root, path) {
  try {
    return readdirSync(join(root, path)) !== undefined;
  } catch {
    return false;
  }
}

/** @param {string} file */
function readJson(file) {
  return JSON.parse(readFileSync(file, 'utf8'));
}

/**
 * Every package `names` reach through their `dependencies`, themselves included.
 *
 * @param {E2eWorld} world
 * @param {Iterable<string>} names
 * @param {'dependencies' | 'allDependencies'} [field]
 */
function packageClosure(world, names, field = 'dependencies') {
  const seen = new Set();
  const pending = [...names];
  for (let name = pending.pop(); name !== undefined; name = pending.pop()) {
    if (seen.has(name)) continue;
    seen.add(name);
    pending.push(...(world.packages.get(name)?.[field] ?? []));
  }
  return seen;
}

/** The app a spec path (`apps/<app>/e2e/...`) belongs to. @param {string} path */
function appOf(path) {
  return /^apps\/([^/]+)\//.exec(path)?.[1];
}

/**
 * The packages each spec depends on: its area's packages and the frame, its own imports'
 * packages and files' packages, and what all of those depend on. A spec the area table does not
 * place depends on everything its app does.
 *
 * @param {E2eWorld} world
 * @returns {Map<string, Set<string>>}
 */
export function specPackages(world) {
  return new Map(
    world.specs.map(spec => [spec, packageClosure(world, directPackages(world, spec))]),
  );
}

/** The packages a spec names itself: its area's (or its app's) and the ones its imports reach. */
function directPackages(world, spec) {
  return new Set([...declaredPackages(world, spec), ...importedPackages(world, spec)]);
}

/**
 * How many dependency steps separate a spec's own packages from `name`: 0 when the spec names it.
 *
 * @param {E2eWorld} world
 * @param {Set<string>} direct
 * @param {string} name
 */
function distanceTo(world, direct, name) {
  let frontier = [...direct];
  const seen = new Set(frontier);
  for (let steps = 0; frontier.length > 0; steps += 1) {
    if (frontier.includes(name)) return steps;
    frontier = frontier
      .flatMap(from => world.packages.get(from)?.dependencies ?? [])
      .filter(next => !seen.has(next) && seen.add(next));
  }
  return Number.POSITIVE_INFINITY;
}

/** The packages the area table, or failing that the app's own dependencies, give a spec. */
function declaredPackages(world, spec) {
  const app = appOf(spec) ?? '';
  const areas = Object.values(world.areas.apps[app] ?? {});
  const placed = areas.filter(({specs}) => specs.some(glob => matchesGlob(spec, glob)));
  if (placed.length > 0) return [...world.areas.frame, ...placed.flatMap(({packages}) => packages)];
  return appPackage(world, app)?.allDependencies ?? [];
}

/** The workspace package of the app `app`. */
function appPackage(world, app) {
  return [...world.packages.values()].find(({directory}) => directory === `apps/${app}`);
}

/** The packages a file imports, directly and through the files it imports. */
function importedPackages(world, file) {
  const packages = new Set();
  for (const reached of importClosure(world, [file])) {
    for (const name of world.imports.get(reached)?.packages ?? []) packages.add(name);
  }
  return packages;
}

/** The files `starts` import, transitively, themselves included. */
function importClosure(world, starts) {
  const seen = new Set();
  const pending = [...starts];
  for (let file = pending.pop(); file !== undefined; file = pending.pop()) {
    if (seen.has(file)) continue;
    seen.add(file);
    pending.push(...(world.imports.get(file)?.files ?? []));
  }
  return seen;
}

/**
 * What one changed file means for the specs, as data: nothing, one spec, the importers of a file,
 * every spec of an app, every spec that depends on a package, or every spec.
 *
 * @param {string} path a repository-relative path
 * @param {E2eWorld} world
 */
function effectOf(path, world) {
  if (!path.startsWith(CLIENTS_PREFIX)) return {kind: 'none'};
  const file = path.slice(CLIENTS_PREFIX.length);
  if (TEST_ONLY_PATH.some(pattern => pattern.test(file))) return {kind: 'none'};
  if (world.specs.includes(file)) return {kind: 'spec', file};
  const inApp = /^apps\/([^/]+)\/e2e\//.exec(file);
  if (inApp !== null || file.startsWith('tools/e2e/'))
    return {kind: 'helper', file, app: inApp?.[1]};
  const owner = [...world.packages.values()]
    .filter(({directory}) => file.startsWith(`${directory}/`))
    .sort((a, b) => b.directory.length - a.directory.length)[0];
  if (owner === undefined)
    return {kind: 'all', why: `${file} is not known to leave the specs alone`};
  const app = appOf(`${owner.directory}/`);
  return app === undefined
    ? {kind: 'package', name: owner.name}
    : {kind: 'app', app, why: `${owner.name} changed`};
}

/**
 * The `[spec, reason, distance]` triples one effect selects; the distance is `null` for direct
 * evidence and the dependency steps from the spec to the changed package otherwise.
 *
 * @param {ReturnType<typeof effectOf>} effect
 * @param {E2eWorld} world
 * @param {Map<string, Set<string>>} needs the packages each spec depends on
 * @returns {[string, string][]}
 */
function specsOf(effect, world, needs) {
  const ofApp = (app, why) =>
    world.specs.filter(spec => appOf(spec) === app).map(spec => [spec, why, null]);
  switch (effect.kind) {
    case 'none':
      return [];
    case 'spec':
      return [[effect.file, `${effect.file} changed`, null]];
    case 'all':
      return world.specs.map(spec => [spec, effect.why, null]);
    case 'app':
      return ofApp(effect.app, effect.why);
    case 'helper': {
      const readers = [...world.imports]
        .filter(([, {assets}]) =>
          (assets ?? []).some(directory => effect.file.startsWith(`${directory}/`)),
        )
        .map(([reader]) => reader);
      const targets = [effect.file, ...readers];
      const importers = world.specs.filter(spec =>
        targets.some(target => importClosure(world, [spec]).has(target)),
      );
      if (importers.length > 0)
        return importers.map(spec => [spec, `imports ${effect.file}`, null]);
      if (effect.app !== undefined) return ofApp(effect.app, `${effect.file} is read by no import`);
      return world.specs.map(spec => [spec, `${effect.file} is shared by every app`, null]);
    }
    default:
      return specsOfPackage(effect.name, world, needs);
  }
}

/** The specs that depend on a package, and every spec of an app that depends on it but no spec does. */
function specsOfPackage(name, world, needs) {
  const reached = world.specs.filter(spec => needs.get(spec)?.has(name));
  const unreached = [...world.packages.values()]
    .filter(({directory}) => directory.startsWith('apps/'))
    .map(({directory}) => directory.slice('apps/'.length))
    .filter(app =>
      packageClosure(world, appPackage(world, app).allDependencies, 'allDependencies').has(name),
    )
    .filter(app => !reached.some(spec => appOf(spec) === app))
    .flatMap(app => world.specs.filter(spec => appOf(spec) === app));
  return [
    ...reached.map(spec => [
      spec,
      `depends on ${name}`,
      distanceTo(world, directPackages(world, spec), name),
    ]),
    ...unreached.map(spec => [
      spec,
      `${name} reaches no spec of its app by a declared relation`,
      null,
    ]),
  ];
}

/**
 * The specs a change can affect, with the reason each is selected.
 *
 * @param {readonly string[]} changed repository-relative paths
 * @param {E2eWorld} world
 * @returns {{specs: string[], reasons: Map<string, string>}}
 */
export function affectedSpecs(changed, world) {
  const needs = specPackages(world);
  const reasons = new Map();
  const distances = new Map();
  for (const path of changed) {
    for (const [spec, reason, distance] of specsOf(effectOf(path, world), world, needs)) {
      if (!reasons.has(spec)) reasons.set(spec, reason);
      // Direct evidence (`null`) outranks any dependency distance; otherwise the closest wins.
      const known = distances.get(spec);
      if (known !== null)
        distances.set(spec, distance === null ? null : Math.min(known ?? distance, distance));
    }
  }
  return {specs: [...reasons.keys()].sort(), reasons, distances};
}

/**
 * The specs a pull request runs and those it leaves to the queue: every spec with direct
 * evidence, then the others closest first while their recorded seconds fit `budget`.
 *
 * @param {{specs: string[], distances: Map<string, number | null>}} selection
 * @param {Record<string, number>} seconds
 * @param {number} [budget]
 */
export function withinBudget(selection, seconds, budget = PACKAGE_SPEC_BUDGET_SECONDS) {
  const weight = spec => seconds[spec] ?? 0;
  const run = selection.specs.filter(spec => selection.distances.get(spec) === null);
  let used = run.reduce((sum, spec) => sum + weight(spec), 0);
  const rest = selection.specs
    .filter(spec => selection.distances.get(spec) !== null)
    .sort(
      (a, b) =>
        selection.distances.get(a) - selection.distances.get(b) ||
        weight(a) - weight(b) ||
        a.localeCompare(b),
    );
  const deferred = [];
  for (const spec of rest) {
    if (used + weight(spec) <= budget) {
      run.push(spec);
      used += weight(spec);
    } else deferred.push(spec);
  }
  return {run: run.sort(), deferred: deferred.sort()};
}

/**
 * How many jobs the specs need: one per `SECONDS_PER_SHARD` of recorded time, at least one when
 * there are specs, at most `MAX_SHARDS`. A spec with no recorded time counts as the median.
 *
 * @param {readonly string[]} specs
 * @param {Record<string, number>} seconds
 */
export function shardsFor(specs, seconds) {
  if (specs.length === 0) return 0;
  const measured = Object.values(seconds).sort((a, b) => a - b);
  const fallback = measured[Math.floor(measured.length / 2)] ?? 1;
  const total = specs.reduce((sum, spec) => sum + (seconds[spec] ?? fallback), 0);
  return Math.min(MAX_SHARDS, Math.max(1, Math.ceil(total / SECONDS_PER_SHARD)));
}

/**
 * Problems with the area table: a spec two areas claim, a glob no spec matches, a package that
 * does not exist. Each names the offender, since a person fixes it by editing the table.
 *
 * @param {E2eWorld} world
 * @returns {string[]}
 */
export function areaErrors(world) {
  const named = [
    ...world.areas.frame,
    ...Object.values(world.areas.apps).flatMap(areas =>
      Object.values(areas).flatMap(({packages}) => packages),
    ),
  ];
  return [
    ...named
      .filter(name => !world.packages.has(name))
      .map(name => `tools/e2e/areas.json: ${name} is not a workspace package`),
    ...Object.entries(world.areas.apps).flatMap(([app, areas]) => appAreaErrors(world, app, areas)),
  ];
}

/** The errors of one app's areas: no specs, a glob that matches none, a spec in two areas. */
function appAreaErrors(world, app, areas) {
  const specs = world.specs.filter(spec => appOf(spec) === app);
  const claims = spec =>
    Object.entries(areas)
      .filter(([, {specs: globs}]) => globs.some(glob => matchesGlob(spec, glob)))
      .map(([area]) => area);
  return [
    ...(specs.length === 0 ? [`tools/e2e/areas.json: ${app} has no e2e specs`] : []),
    ...Object.entries(areas).flatMap(([area, {specs: globs}]) =>
      globs
        .filter(glob => !specs.some(spec => matchesGlob(spec, glob)))
        .map(glob => `tools/e2e/areas.json: ${app}/${area}: ${glob} matches no spec`),
    ),
    ...specs
      .filter(spec => claims(spec).length > 1)
      .map(
        spec =>
          `tools/e2e/areas.json: ${spec} is in ${claims(spec).join(' and ')}; place it in one area`,
      ),
  ];
}

/**
 * Whether a change to `pnpm-lock.yaml` can change what an installed package does: its settings
 * above `importers:` or its resolved packages from `packages:` on. A change only to `importers:`
 * records what a workspace package asks for, which is the change of that package's own
 * `package.json`, selected by its own path.
 *
 * @param {string} before
 * @param {string} after
 */
export function lockfileChangesResolutions(before, after) {
  const outsideImporters = text => {
    const importers = text.indexOf('\nimporters:');
    const packages = text.indexOf('\npackages:');
    if (importers < 0 || packages < importers) return text;
    return text.slice(0, importers) + text.slice(packages);
  };
  return outsideImporters(before) !== outsideImporters(after);
}

/** @param {string[]} argv */
function main(argv) {
  const options = new Map();
  for (let index = 0; index < argv.length; index += 2) options.set(argv[index], argv[index + 1]);
  const world = loadWorld();
  const errors = areaErrors(world);
  if (errors.length > 0) throw new Error(errors.join('\n'));
  const output = options.get('--github-output');
  const mode = options.get('--mode') ?? 'selected';
  let specs;
  let shards;
  if (mode === 'all') {
    specs = [];
    shards = MAX_SHARDS;
    console.log(`e2e: the whole suite across ${shards} jobs`);
  } else {
    const base = options.get('--base') ?? 'origin/main';
    const head = options.get('--head') ?? 'HEAD';
    const git = (...args) => execFileSync('git', args, {cwd: CLIENTS_ROOT, encoding: 'utf8'});
    const mergeBase = git('merge-base', head, base).trim();
    const lockfile = `${CLIENTS_PREFIX}pnpm-lock.yaml`;
    const touched = git('diff', '--name-only', '--no-renames', mergeBase, head)
      .split('\n')
      .filter(Boolean);
    const changed = touched.filter(
      path =>
        path !== lockfile ||
        lockfileChangesResolutions(
          git('show', `${mergeBase}:${lockfile}`),
          git('show', `${head}:${lockfile}`),
        ),
    );
    const selected = affectedSpecs(changed, world);
    const bounded = withinBudget(selected, world.seconds);
    specs = bounded.run.map(spec => spec.slice('apps/'.length));
    shards = shardsFor(bounded.run, world.seconds);
    for (const spec of bounded.run)
      console.log(`e2e: ${spec.slice('apps/'.length)}: ${selected.reasons.get(spec)}`);
    for (const spec of bounded.deferred)
      console.log(
        `e2e: ${spec.slice('apps/'.length)}: left to the merge queue (${selected.reasons.get(spec)}, ${selected.distances.get(spec)} steps away)`,
      );
    console.log(
      `e2e: ${specs.length} of ${world.specs.length} specs across ${shards} jobs; ${bounded.deferred.length} left to the merge queue`,
    );
  }
  if (output !== undefined) {
    appendFileSync(
      output,
      `specs=${JSON.stringify(specs)}\nshards=${JSON.stringify(Array.from({length: shards}, (_, index) => index + 1))}\n`,
    );
  }
}

if (process.argv[1] !== undefined && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main(process.argv.slice(2));
}
