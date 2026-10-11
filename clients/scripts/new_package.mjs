#!/usr/bin/env node

/**
 * `pnpm new:package <layer> <name> [--runtime neutral|dom|node]`: create a workspace package that
 * passes every check on its first commit.
 *
 * The package's `vibesys` block is written first and everything else is derived from it, the same
 * way `scripts/check_package_graph.mjs` reads it: the `exports` map (with the `@vibesys/source`
 * condition), one composite `tsconfig.<entry>.json` per entry with its runtime's `lib` and `types`,
 * the test project and solution file, the scripts, a passing smoke test per entry, and the
 * `tsconfig.architecture.json` path map entries dependency-cruiser resolves. Knip derives its policy
 * from the exports, and Biome and Turborepo cover the package by glob, so neither needs an edit.
 *
 * A feature (`pnpm new:package feature stop-run`) lands in `features/<name>` as
 * `@vibesys/feature-<name>` with `./state`, `./renderer`, and `./main`; an app in `apps/<name>` as
 * `@vibesys/app-<name>`; any other layer in `<name>` as `@vibesys/<name>` with one `.` entry of the
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
const ARCHITECTURE_TSCONFIG = 'tsconfig.architecture.json';
const TYPESCRIPT = '^5.8.3';
const BUN_TYPES = '1.4.2';
const NODE_TYPES = '^24.0.0';

const LIBS = {
  neutral: ['ES2022'],
  dom: ['ES2022', 'DOM', 'DOM.Iterable'],
  node: ['ES2022'],
};
const TYPES = {neutral: [], dom: [], node: ['node']};

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
    directory: app ? `apps/${name}` : name,
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
        `/** The ${plan.packageName} ${label} (${runtime}). */\n` +
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

/** Every file the package needs, as `{path: content}` relative to the package directory. */
export function packageFiles(plan, workspaceRoot) {
  const packageRoot = join(workspaceRoot, plan.directory);
  const base = relative(packageRoot, join(workspaceRoot, 'tsconfig.base.json'));
  const runner = relative(packageRoot, join(workspaceRoot, 'scripts/run_bun_tests.mjs'));
  const usesNode = Object.values(plan.entries).includes('node');
  const entries = Object.keys(plan.entries).map(subpath => [
    subpath,
    entryFiles(plan, subpath, base),
  ]);
  const projects = entries.map(([, entry]) => entry.project);
  const files = Object.assign({}, ...entries.map(([, entry]) => entry.files));
  files['tsconfig.test.json'] = json({
    extends: base,
    compilerOptions: {
      types: usesNode ? ['bun', 'node'] : ['bun'],
      noEmit: true,
      tsBuildInfoFile: '.tsbuild/test.tsbuildinfo',
    },
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
    references: projects.map(project => ({path: `./${project}`})),
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
  config.compilerOptions = {...config.compilerOptions, paths};
  writeFileSync(architecture, json(config));
  return plan.directory;
}

function main(argv) {
  const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
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
