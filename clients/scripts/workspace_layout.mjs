/**
 * The workspace's package enumeration, derived once.
 *
 * `pnpm-workspace.yaml` admits directories rather than names (`packages: ["*"]`), so which
 * packages exist is a fact about the filesystem, not a list to maintain. Every gate that used to
 * repeat the names reads them from here: `.dependency-cruiser.mjs` (the scanned-path patterns),
 * `scripts/check_ts_architecture.mjs` (the scan roots), `scripts/check_ts_package_manifests.mjs`
 * (the dependency policy), and `knip.config.ts` (the audited workspaces). The workspace scripts in
 * `package.json` need no list at all: `pnpm -r` runs every member by construction.
 *
 * Guarantees. `workspaceLayout` returns every directory the workspace file admits that holds a
 * `package.json`, in path order, with the source and tooling directories inside it. It throws,
 * naming the offending path, on a workspace file or manifest it cannot interpret. A directory it
 * does not recognise is reported as tooling rather than dropped, so an unknown directory makes the
 * gates report more, never less. `declarationErrors` reports the two facts that cannot be derived
 * away (a tsconfig path map and the scripts `pnpm -r` drives) when they disagree with that layout,
 * so a package that joins the workspace fails the gate instead of joining it unchecked.
 */

import {existsSync, readdirSync, readFileSync} from 'node:fs';
import {join} from 'node:path';

const WORKSPACE_FILE = 'pnpm-workspace.yaml';
const MANIFEST_FILE = 'package.json';
const ARCHITECTURE_TSCONFIG = 'tsconfig.architecture.json';
const SOURCE_DIRECTORY = 'src';
const BUILD_OUTPUT_PREFIX = './dist/';
const JAVASCRIPT_SUFFIX = '.js';
const TYPESCRIPT_SUFFIX = '.ts';

// Directories that hold no source: `dist` is every package's build output, `node_modules` its
// dependencies, and a leading dot marks a tool cache (`.browser-dist`, `.vibesys-demo`). Anything
// else counts as source, which is the safe direction: an unrecognised directory is scanned.
const NON_SOURCE_DIRECTORIES = new Set(['dist', 'node_modules']);

// A workspace pattern this module understands: `*` (every directory, what pnpm is configured with)
// or a literal directory name. Anything else would need glob semantics to expand correctly, and
// guessing would narrow the package set silently.
const SUPPORTED_PATTERN = /^(?:\*|[\w.-]+)$/;

const SEQUENCE_ENTRY = /^\s+-\s*(?:"([^"]+)"|'([^']+)'|([^\s#]+))\s*$/;

// `pnpm -r <script>` skips a member that does not declare the script, so an undeclared script is
// the same silent gap in build, check, and test coverage that a stale name list used to be.
const REQUIRED_SCRIPTS = ['build', 'check', 'test'];

/**
 * The workspace packages and the source directories inside them. Each package carries its parsed
 * manifest, so the manifest is read and validated once for every gate that reads a field of it.
 *
 * @param {string} root the workspace root (the directory holding `pnpm-workspace.yaml`)
 * @returns {{
 *   packages: {name: string, directory: string, manifest: object}[],
 *   toolingDirectories: string[],
 *   scanRoots: string[],
 * }}
 */
export function workspaceLayout(root) {
  const packages = readPackages(root);
  const packageDirectories = new Set(packages.map(({directory}) => directory));
  const source = [];
  const tooling = [];
  for (const {directory} of packages) {
    for (const child of sourceDirectories(root, directory)) {
      (child === `${directory}/${SOURCE_DIRECTORY}` ? source : tooling).push(child);
    }
  }
  // A root directory that is not a package is the workspace's own tooling (`scripts`).
  for (const child of sourceDirectories(root, '')) {
    if (!packageDirectories.has(child)) tooling.push(child);
  }
  return {
    packages,
    toolingDirectories: tooling.sort(),
    scanRoots: [...source, ...tooling].sort(),
  };
}

/**
 * The directories as one regular-expression alternation, for the path patterns in
 * `.dependency-cruiser.mjs`. The caller wraps it, because a rule that back-references its `from`
 * capture needs a capturing group and the rest do not.
 *
 * @param {string[]} directories
 * @returns {string}
 */
export function pathAlternation(directories) {
  if (directories.length === 0) {
    throw new Error('pathAlternation needs at least one directory');
  }
  return directories
    .map(directory => directory.replaceAll(/[\\^$.*+?()[\]{}|]/g, '\\$&'))
    .join('|');
}

/**
 * The declarations that cannot be derived from the layout and so must agree with it: the
 * architecture tsconfig's path map and the scripts the workspace scripts run.
 *
 * @param {string} root
 * @param {ReturnType<typeof workspaceLayout>} layout
 * @returns {string[]} one message per disagreement, naming the file and the offending key
 */
export function declarationErrors(root, layout) {
  return [...architecturePathErrors(root, layout), ...packageScriptErrors(layout)];
}

function architecturePathErrors(root, layout) {
  const errors = [];
  const declared = readJson(root, ARCHITECTURE_TSCONFIG).compilerOptions?.paths ?? {};
  const expected = expectedArchitecturePaths(layout);
  for (const [specifier, target] of expected) {
    const value = declared[specifier];
    if (!Array.isArray(value) || value.length !== 1 || value[0] !== target) {
      errors.push(`${ARCHITECTURE_TSCONFIG}: ${specifier} must map to ["${target}"]`);
    } else if (!existsSync(join(root, target))) {
      errors.push(`${ARCHITECTURE_TSCONFIG}: ${specifier} maps to missing ${target}`);
    }
  }
  for (const specifier of Object.keys(declared)) {
    if (!expected.has(specifier)) {
      errors.push(`${ARCHITECTURE_TSCONFIG}: ${specifier} is not a workspace package export`);
    }
  }
  return errors;
}

/**
 * The path map the packages imply: every package's public entry points, mapped to the source they
 * are built from, so a public import resolves to source instead of build output.
 */
function expectedArchitecturePaths(layout) {
  const expected = new Map();
  for (const {name, directory, manifest} of layout.packages) {
    const subpaths = manifest.exports;
    if (subpaths === undefined) {
      expected.set(name, `${directory}/${SOURCE_DIRECTORY}/index${TYPESCRIPT_SUFFIX}`);
      continue;
    }
    for (const [subpath, target] of Object.entries(subpaths)) {
      const specifier = subpath === '.' ? name : `${name}/${subpath.slice('./'.length)}`;
      expected.set(specifier, exportSourcePath(directory, subpath, target));
    }
  }
  return expected;
}

function exportSourcePath(directory, subpath, target) {
  const built = typeof target === 'string' ? target : (target?.import ?? target?.default);
  if (
    typeof built !== 'string' ||
    !built.startsWith(BUILD_OUTPUT_PREFIX) ||
    !built.endsWith(JAVASCRIPT_SUFFIX)
  ) {
    throw new Error(
      `${directory}/${MANIFEST_FILE}: cannot map the "${subpath}" export target ` +
        `${JSON.stringify(built)} to a source file`,
    );
  }
  const relativePath = built.slice(BUILD_OUTPUT_PREFIX.length, -JAVASCRIPT_SUFFIX.length);
  return `${directory}/${SOURCE_DIRECTORY}/${relativePath}${TYPESCRIPT_SUFFIX}`;
}

function packageScriptErrors(layout) {
  const errors = [];
  for (const {name, directory, manifest} of layout.packages) {
    const scripts = manifest.scripts ?? {};
    for (const script of REQUIRED_SCRIPTS) {
      if (typeof scripts[script] !== 'string') {
        errors.push(
          `${directory}/${MANIFEST_FILE}: ${name} must declare a "${script}" script, ` +
            `because \`pnpm -r ${script}\` skips a package that does not`,
        );
      }
    }
  }
  return errors;
}

function readPackages(root) {
  const directories = new Set();
  for (const pattern of packagePatterns(root)) {
    if (pattern === '*') {
      for (const candidate of directoryNames(root, '')) {
        if (existsSync(join(root, candidate, MANIFEST_FILE))) directories.add(candidate);
      }
      continue;
    }
    if (!existsSync(join(root, pattern, MANIFEST_FILE))) {
      throw new Error(`${WORKSPACE_FILE}: packages entry "${pattern}" has no ${MANIFEST_FILE}`);
    }
    directories.add(pattern);
  }
  return [...directories].sort().map(directory => readPackage(root, directory));
}

function readPackage(root, directory) {
  const manifest = readJson(root, join(directory, MANIFEST_FILE));
  if (typeof manifest.name !== 'string') {
    throw new Error(`${directory}/${MANIFEST_FILE}: no package name`);
  }
  return {name: manifest.name, directory, manifest};
}

/**
 * The `packages` sequence of `pnpm-workspace.yaml`. Only that key matters here, so this reads the
 * block sequence pnpm writes and rejects any other shape by name rather than falling back to a
 * guess that would narrow the package set.
 */
function packagePatterns(root) {
  const lines = readFileSync(join(root, WORKSPACE_FILE), 'utf8').split('\n');
  const start = lines.findIndex(line => line.trimEnd() === 'packages:');
  if (start < 0) throw new Error(`${WORKSPACE_FILE}: no packages key`);
  const patterns = [];
  for (const line of lines.slice(start + 1)) {
    const trimmed = line.trim();
    if (trimmed === '' || trimmed.startsWith('#')) continue;
    const entry = SEQUENCE_ENTRY.exec(line);
    if (entry === null) break;
    patterns.push(entry[1] ?? entry[2] ?? entry[3]);
  }
  if (patterns.length === 0) throw new Error(`${WORKSPACE_FILE}: packages is empty`);
  for (const pattern of patterns) {
    if (!SUPPORTED_PATTERN.test(pattern)) {
      throw new Error(
        `${WORKSPACE_FILE}: unsupported packages pattern "${pattern}"; ` +
          'scripts/workspace_layout.mjs derives the package set from the workspace root',
      );
    }
  }
  return patterns;
}

function sourceDirectories(root, directory) {
  return directoryNames(root, directory).map(name =>
    directory === '' ? name : `${directory}/${name}`,
  );
}

function directoryNames(root, directory) {
  return readdirSync(join(root, directory), {withFileTypes: true})
    .filter(
      entry =>
        entry.isDirectory() &&
        !entry.name.startsWith('.') &&
        !NON_SOURCE_DIRECTORIES.has(entry.name),
    )
    .map(entry => entry.name)
    .sort();
}

function readJson(root, relativePath) {
  try {
    return JSON.parse(readFileSync(join(root, relativePath), 'utf8'));
  } catch (error) {
    throw new Error(`${relativePath}: cannot read: ${error.message}`);
  }
}
