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
 * Two questions, answered at different layers. **Which directories are packages** is pnpm's
 * question, so this applies pnpm's rule for `*` and nothing else: a direct child of the workspace
 * root that holds a `package.json` and is not one of the two names pnpm never treats as a member.
 * No ignore filter applies there, because `pnpm -r` runs a member's `build`, `check`, and `test`
 * whether or not `.gitignore` happens to name its directory, and a member that runs while no gate
 * can see it is the one failure this module exists to prevent. **Which directories inside a
 * package hold source** is git's question, and that is the only place the ignore filter applies.
 *
 * Guarantees. `workspaceLayout` returns every directory the workspace file admits that holds a
 * `package.json`, in path order, with the source and tooling directories inside it. A symlink to a
 * directory counts, because pnpm treats it as a workspace member and runs its scripts; a link that
 * does not resolve to a directory is dropped, which is what pnpm does with it too. It throws,
 * naming the offending path, on a workspace file or manifest it cannot interpret.
 *
 * Inside that set, a dot-directory and a directory git ignores are left out, and every other
 * directory is reported as tooling rather than dropped, so an unrecognised source directory makes
 * the gates report more, never less. Asking git is what makes that safe rather than merely
 * conservative: untracked output (`dist`, `node_modules`, `web/test-results`, a `coverage/`
 * report, a `web/build` bundle) never reaches the gates at all, so a developer gets the rule set
 * CI has and no red gate with no tracked file to blame. `ignoredDirectories` says what stands in
 * for an answer where git has none.
 *
 * `declarationErrors` reports the two facts that cannot be derived away (a tsconfig path map and
 * the scripts `pnpm -r` drives) when they disagree with that layout, so a package that joins the
 * workspace fails the gate instead of joining it unchecked. A package whose own directory git
 * ignores contributes no scan roots, because everything inside it is ignored too, but it is still
 * a package, so `declarationErrors` names it rather than letting it join unchecked.
 */

import {spawnSync} from 'node:child_process';
import {existsSync, lstatSync, readdirSync, readFileSync, statSync} from 'node:fs';
import {join} from 'node:path';

const WORKSPACE_FILE = 'pnpm-workspace.yaml';
const MANIFEST_FILE = 'package.json';
const ARCHITECTURE_TSCONFIG = 'tsconfig.architecture.json';
const WEB_PACKAGE = '@vibesys/web';
const WEB_TSCONFIG = 'web/tsconfig.json';
const WEB_ALIAS_DEFINITION = 'web/workspace-source-aliases.json';
const SOURCE_DIRECTORY = 'src';
const BUILD_OUTPUT_PREFIX = './dist/';
const JAVASCRIPT_SUFFIX = '.js';
const TYPESCRIPT_SUFFIX = '.ts';

// Which directories hold no source is already declared, in `.gitignore`, so this asks git rather
// than keeping a second list: build output (`dist`), installed dependencies (`node_modules`), tool
// caches, and local tool output (`web/artifacts`, `web/test-results`, `coverage`) are all ignored
// there, and a developer who has run Playwright or a coverage report must get the same rule set CI
// has. git also honours `.git/info/exclude` and `core.excludesFile`, so the declaration is the
// effective rule set rather than `.gitignore` alone; neither is set in this repository, and a
// developer who sets one gets a narrower scan than CI.
const GIT_IGNORE_QUERY = ['check-ignore', '--stdin', '-z'];
// `git check-ignore` exits 0 when some input path is ignored and 1 when none is; any other status
// (128 in a tree with no git metadata) means git could not answer.
const GIT_STATUS_ANSWERED = new Set([0, 1]);

// A leading dot marks version-control or tool metadata (`.git`, `.cache`, `.browser-dist`), never
// a package's source. This is not part of the git question: `.git` holds the answers rather than
// being subject to them, so `git check-ignore` does not report it. pnpm agrees: a direct child
// `.hidden/package.json` is not reported as a workspace member.
const METADATA_PREFIX = '.';

// pnpm's own exclusions from `packages:` globbing, transcribed because the package question is
// pnpm's. Verified against pnpm 11.11.0 with a `package.json` in each of `dist`, `build`,
// `node_modules`, and `bower_components` as direct children: `pnpm -r list` reports `dist` and
// `build` as members and never these two. They belong here and not in `OUTPUT_DIRECTORY_NAMES`
// because they answer "is this a package", not "does this directory hold source".
const NOT_A_WORKSPACE_MEMBER = new Set(['node_modules', 'bower_components']);

// What stands in for an answer where git has none; see `ignoredDirectories` for the two cases. A
// tree with no git metadata is created from tracked files, so the only untracked directories it
// can grow are an install (`node_modules`) and a build (`dist`); the dot-directory caches are
// already excluded above.
const OUTPUT_DIRECTORY_NAMES = new Set(['dist', 'node_modules']);

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
  const rootChildren = candidateChildren(root, '', false);
  const packages = readPackages(root, rootChildren);
  const packageDirectories = new Set(packages.map(({directory}) => directory));
  // The ignore filter starts here, one layer below the package question: a root directory that is
  // not a package, and every directory inside a package. The packages themselves are already
  // settled, by pnpm's rule, and are not re-asked about.
  const candidates = [
    ...rootChildren.filter(({path}) => !packageDirectories.has(path)),
    ...packages.flatMap(({directory}) =>
      candidateChildren(root, directory, crossesSymlink(root, directory)),
    ),
  ];
  const ignored = ignoredDirectories(root, candidates);
  const source = [];
  const tooling = [];
  for (const {path} of candidates) {
    if (ignored.has(path)) continue;
    const separator = path.lastIndexOf('/');
    if (separator < 0) {
      // A root directory that is not a package is the workspace's own tooling (`scripts`).
      tooling.push(path);
    } else {
      (path.slice(separator + 1) === SOURCE_DIRECTORY ? source : tooling).push(path);
    }
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
  return [
    ...architecturePathErrors(root, layout),
    ...workspaceAliasErrors(root, layout),
    ...packageScriptErrors(layout),
  ];
}

/**
 * Check the Web source aliases that both Vite configurations derive from.
 *
 * The package exports are authoritative. `web/tsconfig.json` must map every
 * public entry point of each workspace package Web declares as a dependency,
 * and no other workspace specifier, to its source file. The TypeScript map is
 * checked separately because TypeScript owns its JSONC configuration format.
 *
 * @param {string} root
 * @param {ReturnType<typeof workspaceLayout>} layout
 * @returns {string[]} one message per alias that can resolve to build output
 */
export function workspaceAliasErrors(root, layout) {
  const web = layout.packages.find(({name}) => name === WEB_PACKAGE);
  if (web === undefined) return [];

  const declared = readJson(root, WEB_TSCONFIG).compilerOptions?.paths ?? {};
  const aliases = readJson(root, WEB_ALIAS_DEFINITION);
  const expected = expectedWebPaths(layout, web.manifest);
  return [
    ...pathMapErrors(
      root,
      WEB_TSCONFIG,
      'web',
      declared,
      expected,
      'a declared workspace package export',
    ),
    ...aliasMapErrors(root, aliases, expected),
  ];
}

function architecturePathErrors(root, layout) {
  const declared = readJson(root, ARCHITECTURE_TSCONFIG).compilerOptions?.paths ?? {};
  const expected = expectedArchitecturePaths(layout);
  return pathMapErrors(
    root,
    ARCHITECTURE_TSCONFIG,
    '.',
    declared,
    expected,
    'a workspace package export',
  );
}

function pathMapErrors(root, config, pathBase, declared, expected, unexpectedDescription) {
  const errors = [];
  for (const [specifier, target] of expected) {
    const value = declared[specifier];
    if (!Array.isArray(value) || value.length !== 1 || value[0] !== target) {
      errors.push(`${config}: ${specifier} must map to ["${target}"]`);
    } else if (!existsSync(join(root, pathBase, target))) {
      errors.push(`${config}: ${specifier} maps to missing ${target}`);
    }
  }
  for (const specifier of Object.keys(declared)) {
    if (!expected.has(specifier)) {
      errors.push(`${config}: ${specifier} is not ${unexpectedDescription}`);
    }
  }
  return errors;
}

function aliasMapErrors(root, declared, expected) {
  const errors = [];
  for (const [specifier, target] of expected) {
    if (declared[specifier] !== target) {
      errors.push(`${WEB_ALIAS_DEFINITION}: ${specifier} must map to "${target}"`);
    } else if (!existsSync(join(root, 'web', target))) {
      errors.push(`${WEB_ALIAS_DEFINITION}: ${specifier} maps to missing ${target}`);
    }
  }
  for (const specifier of Object.keys(declared)) {
    if (!expected.has(specifier)) {
      errors.push(
        `${WEB_ALIAS_DEFINITION}: ${specifier} is not a declared workspace package export`,
      );
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

function expectedWebPaths(layout, webManifest) {
  const workspaceDependencies = new Set(
    Object.keys(webManifest.dependencies ?? {}).filter(dependency =>
      dependency.startsWith('@vibesys/'),
    ),
  );
  const allPaths = expectedArchitecturePaths(layout);
  const expected = new Map();
  for (const [specifier, target] of allPaths) {
    if (
      [...workspaceDependencies].some(
        dependency => specifier === dependency || specifier.startsWith(`${dependency}/`),
      )
    ) {
      expected.set(specifier, `../${target}`);
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

/**
 * The directories `*` admits, by pnpm's rule and nothing else: every direct child of the root that
 * holds a `package.json` and that pnpm does not exclude by name.
 *
 * No ignore filter is applied here, deliberately. Filtering the package question by ignore status
 * made a member whose directory `.gitignore` happens to name (`clients/lib`, `clients/env`, and 28
 * other bare directory patterns) run under `pnpm -r` while being invisible to the depcruise scan,
 * the dependency policy, the knip policy, the tsconfig path check, and the required-script
 * meta-check. A nested `dist/package.json` cannot become a member by this rule, because `*` matches
 * direct children only, so keeping the filter out of it costs nothing.
 */
function starMembers(root, rootChildren) {
  return rootChildren
    .map(({path}) => path)
    .filter(path => !NOT_A_WORKSPACE_MEMBER.has(path))
    .filter(path => existsSync(join(root, path, MANIFEST_FILE)));
}

function readPackages(root, rootChildren) {
  const directories = new Set();
  for (const pattern of packagePatterns(root)) {
    if (pattern === '*') {
      for (const directory of starMembers(root, rootChildren)) directories.add(directory);
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

/**
 * The children of `directory` that could hold repository content, as paths relative to `root`.
 * `beyondSymlink` carries whether git has an answer for them; see `ignoredDirectories`.
 *
 * @returns {{path: string, beyondSymlink: boolean}[]}
 */
function candidateChildren(root, directory, beyondSymlink) {
  return readdirSync(join(root, directory), {withFileTypes: true})
    .filter(entry => isContentDirectory(root, directory, entry))
    .map(entry => ({
      path: directory === '' ? entry.name : `${directory}/${entry.name}`,
      beyondSymlink,
    }));
}

/**
 * Whether reaching a path inside `directory` crosses a symlink, which is the one case
 * `git check-ignore` refuses to answer for. Every component is tested, not just the last, so this
 * stays correct if the workspace file ever admits a nested pattern.
 */
function crossesSymlink(root, directory) {
  const components = directory.split('/');
  return components.some((_, index) =>
    lstatSync(join(root, ...components.slice(0, index + 1))).isSymbolicLink(),
  );
}

/**
 * Whether the entry is a directory holding repository content.
 *
 * `readdirSync` has `lstat` semantics, so a symlink to a directory is reported as a symlink and
 * not as a directory. pnpm resolves such a link and runs the linked package's scripts, so dropping
 * it here would run `build`, `check`, and `test` on a package no gate can see.
 */
function isContentDirectory(root, directory, entry) {
  if (entry.name.startsWith(METADATA_PREFIX)) return false;
  if (entry.isDirectory()) return true;
  if (!entry.isSymbolicLink()) return false;
  return resolvesToDirectory(join(root, directory, entry.name));
}

/**
 * Whether `path` resolves to a directory. A link that does not is dropped, however it fails to:
 * it dangles (ENOENT), it loops (ELOOP), or its target's parent is unreadable (EACCES). All three
 * mean the same thing for this question, there is no directory here to enumerate, and pnpm cannot
 * resolve such a link either, so dropping it keeps the derived member set in agreement with
 * pnpm's. Naming the codes instead would be a list to keep in step with libuv for no gain, and
 * `statSync`'s own `{throwIfNoEntry: false}` covers only the first, so a symlink loop used to
 * abort every gate that imports this module with a raw errno.
 */
function resolvesToDirectory(path) {
  try {
    return statSync(path).isDirectory();
  } catch {
    return false;
  }
}

/**
 * The subset of `candidates` that holds no repository content.
 *
 * The question has three answers, not two. `git check-ignore` gives the first two, ignored or not,
 * for every path it can stat. It has none for a path beyond a symlink: it rejects the whole
 * pathspec with `fatal: pathspec '...' is beyond a symbolic link` and exit 128, so one such path
 * costs the answer for every path batched with it. It has none in a tree with no git metadata
 * either. Neither is git failing, so neither is treated as an error: each is classified by
 * `OUTPUT_DIRECTORY_NAMES`, and that fallback is confined to the paths that have no answer so the
 * rest of the workspace keeps git's. Asking about the two together is what let a single symlinked
 * package put `dist`, a `coverage/` report, and a `web/build` bundle back among the scan roots.
 */
function ignoredDirectories(root, candidates) {
  const reachable = candidates.filter(({beyondSymlink}) => !beyondSymlink).map(({path}) => path);
  const beyond = candidates.filter(({beyondSymlink}) => beyondSymlink).map(({path}) => path);
  const answered = reachable.length === 0 ? new Set() : gitIgnoredDirectories(root, reachable);
  if (answered === undefined) return ignoredByName(candidates.map(({path}) => path));
  return new Set([...answered, ...ignoredByName(beyond)]);
}

/** The subset of `paths` git ignores, or `undefined` when git cannot answer for this tree. */
function gitIgnoredDirectories(root, paths) {
  const query = spawnSync('git', GIT_IGNORE_QUERY, {
    cwd: root,
    input: paths.join('\0'),
    encoding: 'utf8',
  });
  if (query.error !== undefined || !GIT_STATUS_ANSWERED.has(query.status)) return undefined;
  return new Set(query.stdout.split('\0').filter(path => path !== ''));
}

/** The stand-in answer, by directory name, for the paths git has none for. */
function ignoredByName(paths) {
  return new Set(
    paths.filter(path => OUTPUT_DIRECTORY_NAMES.has(path.slice(path.lastIndexOf('/') + 1))),
  );
}

function readJson(root, relativePath) {
  try {
    return JSON.parse(readFileSync(join(root, relativePath), 'utf8'));
  } catch (error) {
    throw new Error(`${relativePath}: cannot read: ${error.message}`);
  }
}
