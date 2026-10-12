#!/usr/bin/env node

/**
 * `pnpm new:package <layer> <name> [--runtime neutral|dom|node]`: create a workspace package that
 * passes every check on its first commit.
 *
 * The package's `vibesys` block is written first and everything else is derived from it, the same
 * way `tools/scripts/check_package_graph.mjs` reads it: the `exports` map (with the `@vibesys/source`
 * condition), one composite `tsconfig.<entry>.json` per entry with its runtime's `lib` and `types`,
 * the test project and solution file, the scripts, a passing smoke test per entry, and the
 * `tools/tsconfig.architecture.json` path map entries dependency-cruiser resolves. Knip derives its policy
 * from the exports, and Biome and Turborepo cover the package by glob, so neither needs an edit.
 *
 * A feature (`pnpm new:package feature stop-run`) lands in `features/<name>` as
 * `@vibesys/feature-<name>` with `./state`, `./renderer`, and `./main`, and its `./state` starts
 * from a control-state machine skeleton (`setup({types, actors, actions, guards, delays})` with an
 * injected operation), a pure-transition test through `getNextSnapshot`, and the seeded invariant
 * harness (`checkMachine` from `@vibesys/machine/testing`); an app in `apps/<name>` as
 * `@vibesys/app-<name>`; any other layer (a library: `foundation`, `core`, `domain`, or `service`) in `libs/<name>` as
 * `@vibesys/<name>` with one `.` entry of the
 * given runtime (`neutral` unless `--runtime` says otherwise).
 *
 * Guarantees: it refuses, naming the problem, an unknown layer or runtime, a name that is not
 * lower-case kebab case, and a directory that already exists; it writes nothing in those cases.
 */

import {existsSync, mkdirSync, readFileSync, writeFileSync} from 'node:fs';
import {dirname, join, relative, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {FEATURE_ENTRIES, LAYERS, RUNTIMES} from './check_package_graph.mjs';

const NAME = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/;
const ARCHITECTURE_TSCONFIG = 'tools/tsconfig.architecture.json';
/** The package that owns the machine glue; its manifest pins the one `xstate` version. */
const MACHINE_PACKAGE = 'libs/machine';
const TYPESCRIPT = '^5.8.3';
const BUN_TYPES = '1.4.2';
const NODE_TYPES = '^24.0.0';

const LIBS = {
  neutral: ['ES2022'],
  dom: ['ES2022', 'DOM', 'DOM.Iterable'],
  node: ['ES2022'],
  preload: ['ES2022', 'DOM'],
};
const TYPES = {neutral: [], dom: [], node: ['node'], preload: ['node']};

/**
 * Parse `argv` (without the program name) into a package plan, or throw naming the problem.
 *
 * @returns {{layer: string, name: string, packageName: string, directory: string,
 *   entries: Record<string, string>}}
 */
export function planPackage(argv) {
  const [layer, name, ...options] = argv;
  if (!Object.hasOwn(LAYERS, layer ?? '')) {
    const layers = Object.keys(LAYERS).join('|');
    throw new Error(`usage: pnpm new:package <${layers}> <name> [--runtime <runtime>]`);
  }
  if (!NAME.test(name ?? '')) throw new Error(`${name}: a package name is lower-case kebab case`);
  const runtime = parseRuntime(options);
  if (layer === 'feature') {
    if (runtime !== undefined)
      throw new Error('a feature has fixed entry runtimes; drop --runtime');
    return {
      layer,
      name,
      packageName: `@vibesys/feature-${name}`,
      directory: `features/${name}`,
      entries: {...FEATURE_ENTRIES},
    };
  }
  const app = layer === 'app';
  return {
    layer,
    name,
    packageName: app ? `@vibesys/app-${name}` : `@vibesys/${name}`,
    directory: app ? `apps/${name}` : `libs/${name}`,
    entries: {'.': runtime ?? (app ? 'dom' : 'neutral')},
  };
}

/** The `--runtime` option's value, undefined when absent; throws on anything else. */
function parseRuntime(options) {
  let runtime;
  for (let index = 0; index < options.length; index += 2) {
    if (options[index] !== '--runtime') throw new Error(`${options[index]}: unknown option`);
    runtime = options[index + 1];
  }
  if (runtime !== undefined && !Object.hasOwn(RUNTIMES, runtime)) {
    throw new Error(`--runtime must be one of ${Object.keys(RUNTIMES).join(', ')}`);
  }
  return runtime;
}

/** Where an entry's source lives, relative to `src/`, without extension. */
function entryModule(subpath) {
  return subpath === '.' ? 'index' : `${subpath.slice('./'.length)}/index`;
}

function entryProjectName(subpath) {
  return subpath === '.' ? 'lib' : subpath.slice('./'.length);
}

function json(value) {
  return `${JSON.stringify(value, null, 2)}\n`;
}

/** The files and export one entry needs: its project, its source, and its smoke test. */
function entryFiles(plan, subpath, base) {
  const runtime = plan.entries[subpath];
  const module = entryModule(subpath);
  const projectName = entryProjectName(subpath);
  const sourceDirectory = subpath === '.' ? 'src' : `src/${projectName}`;
  const references =
    plan.layer === 'feature' && subpath !== './state' ? [{path: './tsconfig.state.json'}] : [];
  const identifier = camel(plan.name) + (subpath === '.' ? '' : pascal(projectName));
  const label = subpath === '.' ? 'entry' : `${subpath} entry`;
  return {
    project: `tsconfig.${projectName}.json`,
    exported: {
      '@vibesys/source': `./src/${module}.ts`,
      types: `./dist/${module}.d.ts`,
      default: `./dist/${module}.js`,
    },
    files: {
      [`tsconfig.${projectName}.json`]: json({
        extends: base,
        compilerOptions: {
          lib: LIBS[runtime],
          types: TYPES[runtime],
          ...(runtime === 'dom' ? {jsx: 'react-jsx'} : {}),
          rootDir: 'src',
          outDir: 'dist',
          tsBuildInfoFile: `dist/.${projectName}.tsbuildinfo`,
        },
        include: [`${sourceDirectory}/**/*.ts`, `${sourceDirectory}/**/*.tsx`],
        exclude: [`${sourceDirectory}/**/*.test.ts`, `${sourceDirectory}/**/*.test.tsx`],
        ...(references.length > 0 ? {references} : {}),
      }),
      [`src/${module}.ts`]:
        `/** This package's ${label} (${runtime}). */\n` +
        `export const ${identifier} = '${plan.name}';\n`,
      [`src/${module}.test.ts`]:
        "import {expect, test} from 'bun:test';\n" +
        `import {${identifier}} from './index.js';\n\n` +
        `test('the ${subpath} entry loads', () => {\n` +
        `  expect(${identifier}).toBe('${plan.name}');\n` +
        '});\n',
    },
  };
}

/** The `xstate` version the workspace pins, read from the machine package's manifest. */
function xstateVersion(workspaceRoot) {
  const manifest = JSON.parse(
    readFileSync(join(workspaceRoot, MACHINE_PACKAGE, 'package.json'), 'utf8'),
  );
  return manifest.dependencies.xstate;
}

/** A feature's control-state skeleton: the machine, its tests, and its export. */
function machineFiles(plan) {
  const id = camel(plan.name);
  const type = pascal(plan.name);
  return {
    'src/state/machine.ts': `/**
 * The ${plan.name} feature's control state, as a machine (docs/contributing/clients-architecture.md,
 * "State"). A skeleton to replace: a mode (\`idle\`, \`working\`), the operation \`working\`
 * invokes (provided by \`renderer/effects\` with \`machine.provide({actors})\`), and a deadline as an
 * \`after\` delay. Leaving \`working\` stops the operation and cancels the deadline.
 */
import {unprovided} from '@vibesys/machine';
import {assign, setup} from 'xstate';

export interface ${type}Context {
  readonly attempts: number;
  readonly error: string | null;
}

export type ${type}Event = {readonly type: 'start'} | {readonly type: 'cancel'};

export const ${id}Machine = setup({
  types: {context: {} as ${type}Context, events: {} as ${type}Event},
  actors: {work: unprovided<null>('work')},
  actions: {
    countAttempt: assign(({context}) => ({attempts: context.attempts + 1, error: null})),
    fail: assign((_, params: {readonly error: string}) => ({error: params.error})),
  },
  guards: {mayStart: ({context}) => context.attempts < 3},
  delays: {deadline: 10_000},
}).createMachine({
  id: '${id}',
  context: {attempts: 0, error: null},
  initial: 'idle',
  states: {
    idle: {on: {start: {guard: 'mayStart', target: 'working', actions: 'countAttempt'}}},
    working: {
      invoke: {
        id: 'work',
        src: 'work',
        onDone: 'idle',
        onError: {
          target: 'idle',
          actions: {type: 'fail', params: ({event}) => ({error: String(event.error)})},
        },
      },
      after: {deadline: {target: 'idle', actions: {type: 'fail', params: {error: 'timed out'}}}},
      on: {cancel: 'idle'},
    },
  },
});
`,
    'src/state/machine.test.ts': `import {describe, expect, test} from 'bun:test';
import {checkMachine} from '@vibesys/machine/testing';
import {getInitialSnapshot, getNextSnapshot} from 'xstate';
import {${id}Machine} from './machine.js';

describe('${id}Machine', () => {
  test('start begins the work, and the deadline gives up on it', () => {
    const working = getNextSnapshot(${id}Machine, getInitialSnapshot(${id}Machine), {type: 'start'});
    expect(working.value).toBe('working');
    const timedOut = getNextSnapshot(${id}Machine, working, {
      type: 'xstate.after.deadline.${id}.working',
    } as never);
    expect({value: timedOut.value, error: timedOut.context.error}).toEqual({
      value: 'idle',
      error: 'timed out',
    });
  });

  test('model: work and its deadline live only while working; attempts are bounded', () => {
    checkMachine({
      machine: ${id}Machine,
      input: undefined,
      event: (random, live) =>
        random.pick([
          {type: 'start'},
          {type: 'cancel'},
          ...live.operations.map(operation => live.done(operation, null)),
          ...live.operations.map(operation => live.error(operation, new Error('failed'))),
          ...live.delays.map(delay => live.fire(delay)),
        ]),
      invariants: snapshot => (snapshot.context.attempts <= 3 ? [] : ['more than three attempts']),
    });
  });
});
`,
  };
}

/** Every file the package needs, as `{path: content}` relative to the package directory. */
export function packageFiles(plan, workspaceRoot) {
  const packageRoot = join(workspaceRoot, plan.directory);
  const base = relative(packageRoot, join(workspaceRoot, 'tools/tsconfig.base.json'));
  const runner = relative(packageRoot, join(workspaceRoot, 'tools/scripts/run_bun_tests.mjs'));
  const usesNode = Object.values(plan.entries).some(runtime => TYPES[runtime].includes('node'));
  const entries = Object.keys(plan.entries).map(subpath => [
    subpath,
    entryFiles(plan, subpath, base),
  ]);
  const projects = entries.map(([, entry]) => entry.project);
  const files = Object.assign({}, ...entries.map(([, entry]) => entry.files));
  const feature = plan.layer === 'feature';
  const machine = relative(packageRoot, join(workspaceRoot, MACHINE_PACKAGE));
  if (feature) {
    Object.assign(files, machineFiles(plan));
    files['src/state/index.ts'] += `export {${camel(plan.name)}Machine} from './machine.js';\n`;
    const state = JSON.parse(files['tsconfig.state.json']);
    state.references = [{path: `${machine}/tsconfig.lib.json`}];
    files['tsconfig.state.json'] = json(state);
  }
  files['tsconfig.test.json'] = json({
    extends: base,
    compilerOptions: {
      types: usesNode ? ['bun', 'node'] : ['bun'],
      noEmit: true,
      tsBuildInfoFile: '.tsbuild/test.tsbuildinfo',
    },
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    references: [
      ...projects.map(project => `./${project}`),
      ...(feature ? [`${machine}/tsconfig.testing.json`] : []),
    ].map(path => ({path})),
  });
  files['tsconfig.json'] = json({
    files: [],
    references: [...projects, 'tsconfig.test.json'].map(project => ({path: `./${project}`})),
  });
  files['package.json'] = json({
    name: plan.packageName,
    version: '0.1.0',
    private: true,
    type: 'module',
    vibesys: {layer: plan.layer, entries: plan.entries},
    exports: Object.fromEntries(entries.map(([subpath, entry]) => [subpath, entry.exported])),
    files: ['dist', '!dist/.*.tsbuildinfo'],
    scripts: {
      build: `tsc -b ${projects.join(' ')}`,
      check: 'tsc -b',
      test: `node ${runner} ./src`,
    },
    ...(feature
      ? {
          dependencies: {
            '@vibesys/machine': 'workspace:*',
            xstate: xstateVersion(workspaceRoot),
          },
        }
      : {}),
    devDependencies: {
      '@types/bun': BUN_TYPES,
      ...(usesNode ? {'@types/node': NODE_TYPES} : {}),
      typescript: TYPESCRIPT,
    },
  });
  return files;
}

function camel(name) {
  return name.replace(/-([a-z0-9])/g, (_, letter) => letter.toUpperCase());
}

function pascal(name) {
  const value = camel(name);
  return value.charAt(0).toUpperCase() + value.slice(1);
}

/**
 * Write the package and register its exports in the architecture path map.
 *
 * @param {string} workspaceRoot the workspace root
 * @param {string[]} argv `<layer> <name> [--runtime <runtime>]`
 * @returns {string} the created package directory, relative to the workspace root
 */
export function createPackage(workspaceRoot, argv) {
  const plan = planPackage(argv);
  const packageRoot = join(workspaceRoot, plan.directory);
  if (existsSync(packageRoot)) throw new Error(`${plan.directory} already exists`);
  for (const [path, content] of Object.entries(packageFiles(plan, workspaceRoot))) {
    mkdirSync(dirname(join(packageRoot, path)), {recursive: true});
    writeFileSync(join(packageRoot, path), content);
  }
  const architecture = join(workspaceRoot, ARCHITECTURE_TSCONFIG);
  const config = JSON.parse(readFileSync(architecture, 'utf8'));
  const paths = {...(config.compilerOptions?.paths ?? {})};
  for (const subpath of Object.keys(plan.entries)) {
    const specifier =
      subpath === '.' ? plan.packageName : `${plan.packageName}/${subpath.slice(2)}`;
    paths[specifier] = [`${plan.directory}/src/${entryModule(subpath)}.ts`];
  }
  const sorted = Object.fromEntries(Object.entries(paths).sort(([a], [b]) => a.localeCompare(b)));
  config.compilerOptions = {...config.compilerOptions, paths: sorted};
  // One line per specifier, in specifier order, as the file is kept by hand.
  const text = json(config).replace(/\[\n\s+("[^"]+")\n\s+\]/g, '[$1]');
  writeFileSync(architecture, text);
  return plan.directory;
}

function main(argv) {
  const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
  try {
    const directory = createPackage(root, argv);
    console.log(
      `Created ${directory}. Next: pnpm install, then add workspace dependencies with ` +
        '`pnpm --filter <package> add @vibesys/<dependency>@workspace:*` and a matching project ' +
        'reference; `pnpm check:ts-architecture` names anything missing.',
    );
    return 0;
  } catch (error) {
    console.error(error.message);
    return 1;
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = main(process.argv.slice(2));
}
