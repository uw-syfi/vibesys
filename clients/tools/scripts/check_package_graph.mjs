#!/usr/bin/env node

/**
 * The package-graph gate: every rule about which workspace package may use which, in one place.
 *
 * `docs/contributing/clients-architecture.md` is the design this enforces. Each package declares a
 * layer and a runtime for every entry of its `exports` map in its `package.json`:
 *
 *     "vibesys": {"layer": "core", "entries": {".": "neutral"}}
 *
 * From those declarations, the manifests, the tsconfig projects, and the source on disk, the gate
 * reports one message per violation, prefixed with the rule it breaks (`RULES` lists them):
 *
 * - `declarations`: the `vibesys` block exists, names a known layer and known runtimes, and has no
 *   other keys.
 * - `entries-match-exports`: the `vibesys.entries` keys are exactly the `exports` keys (`.` alone
 *   when a package publishes no exports).
 * - `exports-name-source`: each export lists the `@vibesys/source` condition first, naming a source
 *   file that exists, so dev, tests, and type checks never need a built `dist`.
 * - `feature-entries`: a feature publishes only `./state` (neutral), `./renderer` (dom), and
 *   `./main` (node), and optionally `./styles.css`, its views' stylesheet (a dom asset).
 * - `layer-direction`: a workspace dependency, in any section, is in a strictly lower layer. Peers
 *   share a layer, so this forbids feature-to-feature and app-to-app edges, and cycles.
 * - `workspace-protocol`: a workspace dependency is declared as `workspace:*`.
 * - `imports-are-workspace-packages`: an `@vibesys/*` specifier names a package that exists.
 * - `imports-are-declared`: production source imports only packages in `dependencies`; test code
 *   may also use the other sections.
 * - `imports-use-exports`: a specifier's subpath is one the target's `exports` map publishes.
 * - `runtime-reachability`: production code in an entry imports another package's entry only when
 *   that entry is `neutral` or of the same runtime, so a Node-only package is unreachable from a
 *   `dom` or `neutral` entry.
 * - `external-dependencies`: React only in `dom` entries, Electron only in `node` and `preload`
 *   entries of features and apps, XState only where control state lives (features' `./state` and
 *   `./renderer`, and the effect-owning packages in `MACHINE_PACKAGES`), Node builtins only in
 *   `node` entries (`EXTERNAL_DEPENDENCIES`); a package that declares one of them in
 *   `dependencies` has an entry that may use it.
 * - `preload-sandbox`: a `preload` entry's emitted code loads only Electron. Electron's sandbox
 *   gives a preload a `require` that reaches `electron` and nothing else, so a workspace package is
 *   imported as a type (`import type`, `typeof import(...)`), never as a value.
 * - `entry-projects`: each entry of a package on the per-entry layout is compiled by its own
 *   composite `tsconfig.<entry>.json` (`tsconfig.lib.json` for `.`).
 * - `entry-capability`: that project's `lib` and `types` are its runtime's: no DOM and no Node
 *   types for `neutral`, DOM without Node types for `dom`, Node types without DOM for `node`, both
 *   for `preload`. The compiler then rejects a Node import in a renderer entry.
 * - `references-match-dependencies`: the package's tsconfig projects reference a project of every
 *   workspace dependency and of no other workspace package. A missing reference is not a compiler
 *   error (the import resolves to the dependency's source), so this is where it is caught.
 * - `domain-free`: the production source of a domain-free package (`DOMAIN_FREE`) names no word of
 *   the product's domain (`DOMAIN_WORDS`), in identifiers, strings, or comments, so a primitive
 *   that grows a domain concept is caught where the layer rule alone cannot see it.
 * - `bucket-names`: a foundation, core, or domain package is named for the one concept it owns, so
 *   no segment of its name is a bucket word (`BUCKET_NAMES`: entities, core, common, ...). A bucket
 *   name invites unrelated code in, which the layer rule cannot see.
 * - `layer-matches-folder`: a package's folder agrees with the layer its manifest declares, so the
 *   manifest stays the one source of truth and the folder tree only mirrors it (`LAYER_FOLDERS`):
 *   `libs/*` declares foundation, core, domain, or service; `features/*` declares feature; and
 *   `apps/*` declares app. A package outside those folders fails.
 *
 * Two rules hold the desktop design system for every feature and app package:
 * `strict-no-stylesheets` and `strict-imports`. Biome applies the strict Grit rules to their source.
 *
 * An export whose source is a stylesheet (`./tokens.css`) is a `dom` asset, not a compiled entry:
 * it has no tsconfig project, and bundlers resolve it through the same source condition.
 *
 * Imports are read with TypeScript's pre-processor, so a specifier in a comment or a string is not
 * an import. Resolution-level rules (a relative path into another package, third-party imports)
 * stay in `.dependency-cruiser.mjs`.
 */

import {existsSync, readdirSync, readFileSync} from 'node:fs';
import {builtinModules} from 'node:module';
import {dirname, join, posix, relative, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import ts from 'typescript';
import {workspaceLayout} from './workspace_layout.mjs';

export const SOURCE_CONDITION = '@vibesys/source';
const SCOPE = '@vibesys/';

export const RULES = [
  'declarations',
  'entries-match-exports',
  'exports-name-source',
  'feature-entries',
  'layer-direction',
  'workspace-protocol',
  'imports-are-workspace-packages',
  'imports-are-declared',
  'imports-use-exports',
  'runtime-reachability',
  'external-dependencies',
  'preload-sandbox',
  'entry-projects',
  'entry-capability',
  'references-match-dependencies',
  'domain-free',
  'bucket-names',
  'layer-matches-folder',
  'strict-no-stylesheets',
  'strict-imports',
];

/**
 * The folder each layer's packages live in, directly under the workspace root. The manifest's
 * `vibesys.layer` is the source of truth; this table only says where that layer is filed
 * (`layer-matches-folder`).
 */
export const LAYER_FOLDERS = {
  foundation: 'libs',
  core: 'libs',
  domain: 'libs',
  service: 'libs',
  feature: 'features',
  app: 'apps',
};

/** The keys of a package's `vibesys` block. */
const DECLARED_KEYS = new Set(['layer', 'entries']);

/** Layer ranks. A package may depend only on a strictly lower rank. */
export const LAYERS = {foundation: 0, core: 1, domain: 2, service: 3, feature: 4, app: 5};

/**
 * What each runtime's compiler program may see. A `preload` is an Electron sandboxed preload: it
 * runs in the page's renderer process before the page, with the DOM and Electron's renderer APIs
 * (whose types need Node's), but its `require` reaches only `electron` (`preload-sandbox`), and it
 * may import no Node builtin and no `node` entry.
 */
export const RUNTIMES = {
  neutral: {dom: false, node: false},
  dom: {dom: true, node: false},
  node: {dom: false, node: true},
  preload: {dom: true, node: true},
};

/** A feature's possible entries and their fixed runtimes. */
export const FEATURE_ENTRIES = {'./state': 'neutral', './renderer': 'dom', './main': 'node'};
/** A feature may also publish its views' stylesheet, a `dom` asset its apps import. */
const FEATURE_STYLESHEET = {'./styles.css': 'dom'};

/**
 * The packages that own effects and run control-state machines outside a feature
 * (docs/contributing/clients-architecture.md, "State"): the machine glue and its test kit, and the
 * desktop connection manager (its host connection machine), the live run session (its session
 * machine), and the log follower (one machine per stream on screen). Folds (`store`,
 * `run-state`), models (`hosts`, `runs`), primitives (`ui`), and the wire packages (`contracts`,
 * `transport`, `ipc`) never import XState.
 */
export const MACHINE_PACKAGES = [
  '@vibesys/machine',
  '@vibesys/connection',
  '@vibesys/run-session',
  '@vibesys/logs',
  '@vibesys/run-skeleton',
  '@vibesys/subscription',
];
const ALL_RUNTIMES = Object.keys(RUNTIMES);

/**
 * Third-party imports confined by runtime and layer, the one allowlist for them. A specifier
 * matches a rule when it is the package or one of its subpaths, and a package may appear in more
 * than one rule: an import is allowed when any of its rules allows it. A rule allows an importing
 * entry with one of its `runtimes`, and, when given, of a package in one of its `layers`, at one of
 * its `subpaths`, or named in its `names`. Node builtins (`node:*` and the bare names in
 * `builtinModules`) are confined to `node` entries the same way.
 */
export const EXTERNAL_DEPENDENCIES = [
  {packages: ['react', 'react-dom'], runtimes: ['dom']},
  {packages: ['electron'], runtimes: ['node', 'preload'], layers: ['feature', 'app']},
  {
    packages: ['xstate'],
    runtimes: ALL_RUNTIMES,
    layers: ['feature'],
    subpaths: ['./state', './renderer'],
  },
  {packages: ['xstate'], runtimes: ALL_RUNTIMES, names: MACHINE_PACKAGES},
];
const NODE_BUILTINS = {runtimes: ['node']};
/** What a sandboxed preload's `require` reaches. */
const PRELOAD_MODULES = new Set(['electron']);

/**
 * Packages that predate the per-entry layout, with the rules they skip until the item named in
 * the migration table moves them. The per-entry project rules (`entry-projects` stands for all
 * three) cannot apply to their single tsconfig per package. Every other rule applies. This table
 * only shrinks.
 */
export const LEGACY_EXEMPTIONS = {};

/**
 * Packages whose production source must stay free of the product's domain, and the words that
 * name it. Matching is per word: identifiers are split at case changes (`runStatus` is `run` and
 * `status`), and a plural `s` is dropped.
 */
export const DOMAIN_FREE = new Set(['@vibesys/ui', '@vibesys/store']);
export const DOMAIN_WORDS = new Set([
  'agent',
  'candidate',
  'checkout',
  'experiment',
  'gateway',
  'host',
  'instance',
  'listing',
  'project',
  'replay',
  'resume',
  'round',
  'run',
  'ssh',
  'task',
  'transcript',
  'vibesys',
  'welcome',
]);

/**
 * Words no foundation, core, or domain package name may contain as a segment: each names a place to
 * put things rather than a concept, so the package it names grows into a junk drawer.
 */
export const BUCKET_NAMES = new Set([
  'common',
  'core',
  'entities',
  'helpers',
  'misc',
  'shared',
  'utils',
]);
const NAMED_LAYERS = new Set(['foundation', 'core', 'domain']);

const STYLESHEET = /\.css$/;

function ruleApplies(skipped) {
  return error => !skipped.has(error.slice(0, error.indexOf(':')));
}

const DEPENDENCY_SECTIONS = [
  'dependencies',
  'devDependencies',
  'optionalDependencies',
  'peerDependencies',
];
const SOURCE_FILE = /\.(?:[cm]?ts|tsx)$/;
const DECLARATION_FILE = /\.d\.[cm]?ts$/;
const TEST_FILE = /\.test(?:-helper)?\.[cm]?tsx?$/;
const TEST_DIRECTORIES = new Set(['test-support', 'testing']);
const SKIPPED_DIRECTORIES = new Set(['node_modules', 'dist']);
const NODE_TYPES = new Set(['node', 'bun', 'electron']);

/**
 * Every package-graph violation in the workspace at `root`.
 *
 * @param {string} root the workspace root (the directory holding `pnpm-workspace.yaml`)
 * @returns {string[]} one `<rule>: <path>: <message>` line per violation
 */
export function packageGraphErrors(root) {
  const {packages} = workspaceLayout(root);
  const byName = new Map(packages.map(entry => [entry.name, entry]));
  const errors = [];
  for (const workspacePackage of packages) {
    const declared = declarationErrors(root, workspacePackage);
    errors.push(...declared);
    // Every later rule reads the declarations, so a package whose declarations are wrong is
    // reported for that alone rather than for a cascade of consequences.
    if (declared.length > 0) continue;
    const skipped = LEGACY_EXEMPTIONS[workspacePackage.name] ?? new Set();
    errors.push(...folderErrors(workspacePackage));
    errors.push(...layerErrors(workspacePackage, byName));
    errors.push(...externalManifestErrors(workspacePackage).filter(ruleApplies(skipped)));
    errors.push(...importErrors(root, workspacePackage, byName).filter(ruleApplies(skipped)));
    if (!skipped.has('entry-projects')) {
      errors.push(...projectErrors(root, workspacePackage, byName));
    }
    if (DOMAIN_FREE.has(workspacePackage.name)) {
      errors.push(...domainWordErrors(root, workspacePackage));
    }
    errors.push(...bucketNameErrors(workspacePackage));
    if (isDesignPackage(workspacePackage)) {
      errors.push(...strictErrors(root, workspacePackage, byName));
    }
  }
  return errors;
}

function violation(rule, where, message) {
  return `${rule}: ${where}: ${message}`;
}

/** The package's entries as `{subpath, runtime, source}`, `source` package-relative or undefined. */
export function packageEntries({manifest}) {
  const exportsMap = manifest.exports ?? {};
  return Object.entries(manifest.vibesys.entries).map(([subpath, runtime]) => ({
    subpath,
    runtime,
    source: sourceTarget(exportsMap[subpath]),
  }));
}

function sourceTarget(target) {
  if (target === null || typeof target !== 'object') return undefined;
  const source = target[SOURCE_CONDITION];
  return typeof source === 'string' ? source.replace(/^\.\//, '') : undefined;
}

// --- declarations -----------------------------------------------------------------------------

function declarationErrors(root, {name, directory, manifest}) {
  const where = `${directory}/package.json`;
  const metadata = manifest.vibesys;
  if (metadata === null || typeof metadata !== 'object' || Array.isArray(metadata)) {
    return [violation('declarations', where, `${name} has no "vibesys" block`)];
  }
  const errors = [];
  for (const key of Object.keys(metadata)) {
    if (!DECLARED_KEYS.has(key)) {
      errors.push(violation('declarations', where, `unknown key vibesys.${key}`));
    }
  }
  if (!Object.hasOwn(LAYERS, metadata.layer)) {
    const layers = Object.keys(LAYERS).join(', ');
    errors.push(
      violation(
        'declarations',
        where,
        `vibesys.layer must be one of ${layers}, not ${metadata.layer}`,
      ),
    );
  }
  const entries = metadata.entries;
  if (entries === null || typeof entries !== 'object' || Array.isArray(entries)) {
    errors.push(
      violation('declarations', where, 'vibesys.entries must map each export to a runtime'),
    );
    return errors;
  }
  for (const [subpath, runtime] of Object.entries(entries)) {
    if (!Object.hasOwn(RUNTIMES, runtime)) {
      const runtimes = Object.keys(RUNTIMES).join(', ');
      errors.push(
        violation(
          'declarations',
          where,
          `vibesys.entries["${subpath}"] must be one of ${runtimes}`,
        ),
      );
    }
  }
  if (errors.length > 0) return errors;
  errors.push(...exportErrors(root, where, directory, manifest));
  if (metadata.layer === 'feature') errors.push(...featureEntryErrors(where, entries));
  return errors;
}

function exportErrors(root, where, directory, manifest) {
  const errors = [];
  const entries = Object.keys(manifest.vibesys.entries).sort();
  const exportsMap = manifest.exports ?? {};
  const exported = Object.keys(exportsMap).length === 0 ? ['.'] : Object.keys(exportsMap).sort();
  if (entries.join() !== exported.join()) {
    errors.push(
      violation(
        'entries-match-exports',
        where,
        `vibesys.entries keys (${entries.join(', ')}) must be the exports keys (${exported.join(', ')})`,
      ),
    );
  }
  for (const [subpath, target] of Object.entries(exportsMap)) {
    const source = sourceTarget(target);
    if (source === undefined) {
      errors.push(
        violation(
          'exports-name-source',
          where,
          `exports["${subpath}"] has no "${SOURCE_CONDITION}"`,
        ),
      );
    } else if (Object.keys(target)[0] !== SOURCE_CONDITION) {
      errors.push(
        violation('exports-name-source', where, `exports["${subpath}"] must list it first`),
      );
    } else if (!existsSync(join(root, directory, source))) {
      errors.push(
        violation('exports-name-source', where, `exports["${subpath}"] names missing ${source}`),
      );
    }
  }
  return errors;
}

function featureEntryErrors(where, entries) {
  const errors = [];
  const allowedEntries = {...FEATURE_ENTRIES, ...FEATURE_STYLESHEET};
  for (const [subpath, runtime] of Object.entries(entries)) {
    const expected = allowedEntries[subpath];
    if (expected === undefined) {
      const allowed = Object.keys(allowedEntries).join(', ');
      errors.push(
        violation('feature-entries', where, `a feature exports only ${allowed}, not "${subpath}"`),
      );
    } else if (runtime !== expected) {
      errors.push(
        violation(
          'feature-entries',
          where,
          `a feature's ${subpath} is ${expected}, not ${runtime}`,
        ),
      );
    }
  }
  return errors;
}

// --- folders ----------------------------------------------------------------------------------

function folderErrors({name, directory, manifest}) {
  const where = `${directory}/package.json`;
  const layer = manifest.vibesys.layer;
  const expected = LAYER_FOLDERS[layer];
  const folder = directory.includes('/') ? directory.slice(0, directory.indexOf('/')) : undefined;
  if (folder === undefined || !Object.values(LAYER_FOLDERS).includes(folder)) {
    const folders = [...new Set(Object.values(LAYER_FOLDERS))].map(each => `${each}/`).join(', ');
    return [
      violation(
        'layer-matches-folder',
        where,
        `${name} is in ${directory}/, outside ${folders}; move it to ${expected}/ for its ` +
          `${layer} layer`,
      ),
    ];
  }
  if (folder === expected) return [];
  return [
    violation(
      'layer-matches-folder',
      where,
      `${name} declares layer ${layer}, which belongs in ${expected}/, but sits in ${folder}/; ` +
        'move the package, or correct vibesys.layer if the folder is right',
    ),
  ];
}

// --- layers -----------------------------------------------------------------------------------

function workspaceDependencies(manifest, byName) {
  return DEPENDENCY_SECTIONS.flatMap(section =>
    Object.keys(manifest[section] ?? {})
      .filter(dependency => byName.has(dependency))
      .map(dependency => ({dependency, section})),
  );
}

function layerErrors({name, directory, manifest}, byName) {
  const where = `${directory}/package.json`;
  const layer = manifest.vibesys.layer;
  const errors = [];
  for (const {dependency, section} of workspaceDependencies(manifest, byName)) {
    const target = byName.get(dependency).manifest.vibesys?.layer;
    if (Object.hasOwn(LAYERS, target) && LAYERS[target] >= LAYERS[layer]) {
      const peers = target === layer ? `; no ${layer} may depend on another ${layer}` : '';
      errors.push(
        violation(
          'layer-direction',
          where,
          `${name} (${layer}) must not depend on ${dependency} (${target}) in ${section}; ` +
            `a package depends only on lower layers${peers}`,
        ),
      );
    }
    const version = manifest[section][dependency];
    if (version !== 'workspace:*') {
      errors.push(
        violation(
          'workspace-protocol',
          where,
          `${dependency} must be "workspace:*", not "${version}"`,
        ),
      );
    }
  }
  return errors;
}

// --- imports ----------------------------------------------------------------------------------

/** Files under the package that `accept` their name, package-relative, skipping installs, output, and dot-dirs. */
function filesWhere(packageRoot, accept, relativeDirectory = '') {
  const files = [];
  for (const entry of readdirSync(join(packageRoot, relativeDirectory), {withFileTypes: true})) {
    if (entry.name.startsWith('.') || SKIPPED_DIRECTORIES.has(entry.name)) continue;
    const path = relativeDirectory === '' ? entry.name : `${relativeDirectory}/${entry.name}`;
    if (entry.isDirectory()) files.push(...filesWhere(packageRoot, accept, path));
    else if (accept(entry.name)) files.push(path);
  }
  return files;
}

const isSourceFile = name => SOURCE_FILE.test(name) && !DECLARATION_FILE.test(name);

/** Source files under the package, package-relative. */
function sourceFiles(packageRoot) {
  return filesWhere(packageRoot, isSourceFile);
}

/** Test code: a test file, a test-support directory, or anything outside `src/` (tooling). */
function isTestCode(path) {
  if (TEST_FILE.test(path) || !path.startsWith('src/')) return true;
  return path.split('/').some(segment => TEST_DIRECTORIES.has(segment));
}

/**
 * The entry a production file belongs to: the entry whose source is the file itself, else the
 * entry whose `index` source sits in the deepest directory containing the file. An entry named by
 * a single file (`./websocket` -> `src/websocket.ts`) owns only that file.
 */
export function owningEntry(entries, path) {
  const exact = entries.find(({source}) => source === path);
  if (exact !== undefined) return exact;
  let best;
  for (const entry of entries) {
    const source = entry.source ?? 'src/index.ts';
    if (!/(?:^|\/)index\.[cm]?tsx?$/.test(source)) continue;
    const prefix = `${posix.dirname(source)}/`;
    if (path.startsWith(prefix) && (best === undefined || prefix.length > best.prefix.length)) {
      best = {entry, prefix};
    }
  }
  return best?.entry;
}

/** The bare specifiers (no relative paths) a source text imports, re-exports, or loads. */
export function importedSpecifiers(text) {
  const {importedFiles} = ts.preProcessFile(text, true, true);
  return importedFiles.map(({fileName}) => fileName).filter(name => !name.startsWith('.'));
}

/** The bare specifiers a source text imports, each with its 1-based line. */
export function importedSpecifierLines(text) {
  const {importedFiles} = ts.preProcessFile(text, true, true);
  return importedFiles
    .filter(({fileName}) => !fileName.startsWith('.'))
    .map(({fileName, pos}) => ({
      specifier: fileName,
      line: text.slice(0, pos).split('\n').length,
    }));
}

/** The `@vibesys/*` specifiers among them. */
export function workspaceSpecifiers(text) {
  return importedSpecifiers(text).filter(name => name.startsWith(SCOPE));
}

/** The confinement for a third-party specifier, or undefined when it is unconfined. */
export function externalPolicy(specifier) {
  if (specifier.startsWith('node:') || builtinModules.includes(specifier.split('/')[0])) {
    return {
      name: specifier.startsWith('node:') ? specifier : `node:${specifier}`,
      rules: [NODE_BUILTINS],
    };
  }
  const rules = EXTERNAL_DEPENDENCIES.filter(({packages}) =>
    packages.some(name => specifier === name || specifier.startsWith(`${name}/`)),
  );
  return rules.length === 0 ? undefined : {name: specifier, rules};
}

function externalManifestErrors(workspacePackage) {
  const {directory, manifest} = workspacePackage;
  const entries = packageEntries(workspacePackage);
  const errors = [];
  for (const dependency of Object.keys(manifest.dependencies ?? {})) {
    const policy = externalPolicy(dependency);
    if (policy === undefined) continue;
    const allowed = entries.some(entry => policyAllows(policy, workspacePackage, entry));
    if (!allowed) {
      errors.push(
        violation(
          'external-dependencies',
          `${directory}/package.json`,
          `declares ${dependency}, which no entry of a ${manifest.vibesys.layer} with runtimes ` +
            `${entries.map(({runtime}) => runtime).join(', ')} may use`,
        ),
      );
    }
  }
  return errors;
}

function policyAllows(policy, workspacePackage, entry) {
  return policy.rules.some(rule => ruleAllows(rule, workspacePackage, entry));
}

function ruleAllows(rule, {name, manifest}, entry) {
  return (
    rule.runtimes.includes(entry.runtime) &&
    (rule.layers === undefined || rule.layers.includes(manifest.vibesys.layer)) &&
    (rule.subpaths === undefined || rule.subpaths.includes(entry.subpath)) &&
    (rule.names === undefined || rule.names.includes(name))
  );
}

/** Who a policy allows, in words: one phrase per rule. */
function allowedImporters(policy) {
  return policy.rules
    .map(rule => {
      const runtimes = rule.runtimes === ALL_RUNTIMES ? '' : `${rule.runtimes.join(' or ')} `;
      const layers = rule.layers === undefined ? '' : ` of a ${rule.layers.join(' or ')}`;
      const subpaths = rule.subpaths === undefined ? '' : ` (${rule.subpaths.join(', ')})`;
      const names = rule.names === undefined ? '' : ` of ${rule.names.join(', ')}`;
      return `${runtimes}entries${layers}${subpaths}${names}`;
    })
    .join('; or ');
}

function splitSpecifier(specifier) {
  const [scope, base, ...rest] = specifier.split('/');
  return {name: `${scope}/${base}`, subpath: rest.length === 0 ? '.' : `./${rest.join('/')}`};
}

function importErrors(root, workspacePackage, byName) {
  const {directory} = workspacePackage;
  const packageRoot = join(root, directory);
  const errors = [];
  for (const path of sourceFiles(packageRoot)) {
    const text = readFileSync(join(packageRoot, path), 'utf8');
    for (const specifier of importedSpecifiers(text)) {
      const error = specifier.startsWith(SCOPE)
        ? specifierError(workspacePackage, path, specifier, byName)
        : externalError(workspacePackage, path, specifier);
      if (error !== undefined) errors.push(error);
    }
    errors.push(...preloadErrors(workspacePackage, path, text));
  }
  return errors;
}

/**
 * The modules a source text loads at run time: the specifiers its emitted CommonJS still requires,
 * so `import type` and `typeof import(...)`, which the compiler erases, are not loads.
 */
export function loadedSpecifiers(text, fileName) {
  const {outputText} = ts.transpileModule(text, {
    fileName,
    compilerOptions: {module: ts.ModuleKind.CommonJS, verbatimModuleSyntax: true},
    reportDiagnostics: false,
  });
  return importedSpecifiers(outputText);
}

function preloadErrors(workspacePackage, path, text) {
  if (isTestCode(path)) return [];
  const importer = owningEntry(packageEntries(workspacePackage), path);
  if (importer?.runtime !== 'preload') return [];
  return loadedSpecifiers(text, path)
    .filter(specifier => !PRELOAD_MODULES.has(specifier))
    .map(specifier =>
      violation(
        'preload-sandbox',
        `${workspacePackage.directory}/${path}`,
        `the preload ${workspacePackage.name} ${importer.subpath} loads ${specifier} at run time, ` +
          `but a sandboxed preload's require reaches only ${[...PRELOAD_MODULES].join(', ')}; ` +
          'import it as a type',
      ),
    );
}

function externalError(workspacePackage, path, specifier) {
  const policy = externalPolicy(specifier);
  if (policy === undefined || isTestCode(path)) return undefined;
  const importer = owningEntry(packageEntries(workspacePackage), path);
  if (importer === undefined || policyAllows(policy, workspacePackage, importer)) return undefined;
  return violation(
    'external-dependencies',
    `${workspacePackage.directory}/${path}`,
    `the ${importer.runtime} entry ${workspacePackage.name} ${importer.subpath} must not import ` +
      `${specifier}; only ${allowedImporters(policy)} may`,
  );
}

function specifierError(workspacePackage, path, specifier, byName) {
  const {name, directory, manifest} = workspacePackage;
  const where = `${directory}/${path}`;
  const target = splitSpecifier(specifier);
  if (target.name === name) return undefined;
  const targetPackage = byName.get(target.name);
  if (targetPackage === undefined) {
    return violation('imports-are-workspace-packages', where, `${specifier} names no package`);
  }
  const test = isTestCode(path);
  const sections = test ? DEPENDENCY_SECTIONS : ['dependencies'];
  if (!sections.some(section => Object.hasOwn(manifest[section] ?? {}, target.name))) {
    return violation(
      'imports-are-declared',
      where,
      `imports ${specifier} without declaring ${target.name} in ${sections.join(' or ')}`,
    );
  }
  const published = Object.keys(targetPackage.manifest.exports ?? {});
  if (!published.includes(target.subpath)) {
    return violation(
      'imports-use-exports',
      where,
      `${target.name} does not export "${target.subpath}" (it exports ${published.join(', ') || 'nothing'})`,
    );
  }
  if (test) return undefined;
  const importer = owningEntry(packageEntries(workspacePackage), path);
  const exported = packageEntries(targetPackage).find(({subpath}) => subpath === target.subpath);
  if (importer === undefined || exported === undefined) return undefined;
  if (exported.runtime === 'neutral' || exported.runtime === importer.runtime) return undefined;
  return violation(
    'runtime-reachability',
    where,
    `the ${importer.runtime} entry ${name} ${importer.subpath} must not import ${specifier}, ` +
      `a ${exported.runtime} entry`,
  );
}

// --- compiler capability ----------------------------------------------------------------------

/** The project compiling an entry: `tsconfig.lib.json` for `.`, else `tsconfig.<name>.json`. */
export function entryProject(subpath) {
  return subpath === '.' ? 'tsconfig.lib.json' : `tsconfig.${subpath.slice('./'.length)}.json`;
}

function parsedProject(path) {
  const host = {
    ...ts.sys,
    onUnRecoverableConfigFileDiagnostic: diagnostic => {
      throw new Error(`${path}: ${ts.flattenDiagnosticMessageText(diagnostic.messageText, '\n')}`);
    },
  };
  return ts.getParsedCommandLineOfConfigFile(path, {}, host);
}

function capabilityErrors(where, runtime, options) {
  const errors = [];
  const capability = RUNTIMES[runtime];
  if (options.composite !== true) {
    errors.push(violation('entry-projects', where, 'must be a composite project'));
  }
  if (options.lib === undefined || options.types === undefined) {
    errors.push(
      violation('entry-capability', where, 'must set lib and types, so nothing is implied'),
    );
    return errors;
  }
  const dom = options.lib.some(library => /lib\.dom/i.test(library));
  const node = options.types.some(type => NODE_TYPES.has(type));
  if (dom !== capability.dom) {
    const verb = capability.dom ? 'needs' : 'must not have';
    errors.push(violation('entry-capability', where, `a ${runtime} entry ${verb} the DOM library`));
  }
  if (node !== capability.node) {
    const verb = capability.node ? 'needs' : 'must not have';
    errors.push(
      violation(
        'entry-capability',
        where,
        `a ${runtime} entry ${verb} Node types (types: ${options.types.join(', ') || 'none'})`,
      ),
    );
  }
  return errors;
}

/** The project rules for one entry: a stylesheet is a `dom` asset; anything else has its project. */
function entryProjectErrors(packageRoot, directory, entry) {
  if (STYLESHEET.test(entry.source ?? '')) {
    if (entry.runtime === 'dom') return [];
    return [
      violation(
        'entry-capability',
        `${directory}/package.json`,
        `the stylesheet entry ${entry.subpath} must be dom, not ${entry.runtime}`,
      ),
    ];
  }
  const file = entryProject(entry.subpath);
  const where = `${directory}/${file}`;
  if (!existsSync(join(packageRoot, file))) {
    return [violation('entry-projects', where, `missing; it compiles the ${entry.subpath} entry`)];
  }
  return capabilityErrors(where, entry.runtime, parsedProject(join(packageRoot, file)).options);
}

function projectErrors(root, workspacePackage, byName) {
  const {directory, manifest} = workspacePackage;
  const packageRoot = join(root, directory);
  const errors = packageEntries(workspacePackage).flatMap(entry =>
    entryProjectErrors(packageRoot, directory, entry),
  );
  const referenced = new Set();
  for (const file of readdirSync(packageRoot).filter(file =>
    /^tsconfig(?:\..+)?\.json$/.test(file),
  )) {
    for (const reference of parsedProject(join(packageRoot, file)).projectReferences ?? []) {
      referenced.add(relative(root, reference.path).split('\\').join('/'));
    }
  }
  const owners = [...byName.values()].filter(other => other.directory !== directory);
  const referencedPackages = new Set(
    [...referenced]
      .map(path => owners.find(other => path.startsWith(`${other.directory}/`))?.name)
      .filter(owner => owner !== undefined),
  );
  const dependencies = new Set(
    workspaceDependencies(manifest, byName).map(({dependency}) => dependency),
  );
  for (const dependency of dependencies) {
    if (!referencedPackages.has(dependency)) {
      errors.push(
        violation(
          'references-match-dependencies',
          directory,
          `declares ${dependency} but no tsconfig references a project of it`,
        ),
      );
    }
  }
  for (const owner of referencedPackages) {
    if (!dependencies.has(owner)) {
      errors.push(
        violation(
          'references-match-dependencies',
          directory,
          `references ${owner} without declaring it`,
        ),
      );
    }
  }
  return errors;
}

// --- domain-free packages ---------------------------------------------------------------------

/**
 * The stems a word may be an inflection of: the word, without a plural `s`, and without `ing`
 * (undoubling a final consonant, so `running` is `run`). Not `ed`: `rounded` names a shape.
 */
function stems(word) {
  const candidates = [word, word.replace(/s$/, '')];
  if (word.endsWith('ing') && word.length > 5) {
    const stem = word.slice(0, -3);
    candidates.push(stem, stem.replace(/([b-df-hj-np-tv-z])\1$/, '$1'));
  }
  return candidates;
}

/** The domain words in `text`: its words split at case changes, lower-cased, without inflection. */
export function domainWords(text) {
  const found = new Set();
  for (const word of text.replace(/([a-z0-9])([A-Z])/g, '$1 $2').split(/[^A-Za-z]+/)) {
    const match = stems(word.toLowerCase()).find(stem => DOMAIN_WORDS.has(stem));
    if (match !== undefined) found.add(match);
  }
  return [...found];
}

function domainWordErrors(root, workspacePackage) {
  const {name, directory} = workspacePackage;
  const packageRoot = join(root, directory);
  const stylesheets = packageEntries(workspacePackage)
    .map(({source}) => source)
    .filter(source => STYLESHEET.test(source ?? '') && existsSync(join(packageRoot, source)));
  const errors = [];
  for (const path of [
    ...sourceFiles(packageRoot).filter(file => !isTestCode(file)),
    ...stylesheets,
  ]) {
    const words = domainWords(readFileSync(join(packageRoot, path), 'utf8'));
    if (words.length > 0) {
      errors.push(
        violation(
          'domain-free',
          `${directory}/${path}`,
          `${name} is domain-free, but this names ${words.join(', ')}; ` +
            'keep the concept in the package that owns it and pass data in as props',
        ),
      );
    }
  }
  return errors;
}

// --- bucket names -----------------------------------------------------------------------------

function bucketNameErrors({name, directory, manifest}) {
  if (!NAMED_LAYERS.has(manifest.vibesys.layer)) return [];
  const buckets = name
    .slice(SCOPE.length)
    .split('-')
    .filter(segment => BUCKET_NAMES.has(segment));
  if (buckets.length === 0) return [];
  return [
    violation(
      'bucket-names',
      `${directory}/package.json`,
      `${name} (${manifest.vibesys.layer}) is named for a bucket (${buckets.join(', ')}), not the ` +
        'concept it owns; name it for that concept, or split it into one package per concept',
    ),
  ];
}

// --- design rules for feature and app packages ------------------------------------------------

/**
 * The workspace packages a feature or app imports, with the entries it may import (`null`: any
 * entry the package publishes). UI comes only from `@vibesys/ui`'s `.` and `./tokens`; the rest are
 * the non-UI libraries a screen composes.
 */
export const STRICT_WORKSPACE_IMPORTS = {
  '@vibesys/ui': ['.', './tokens'],
  '@vibesys/contracts': null,
  '@vibesys/store': null,
  '@vibesys/machine': null,
  '@vibesys/problems': null,
  '@vibesys/request-identity': ['.'],
  '@vibesys/run-state': null,
  '@vibesys/run-skeleton': null,
  '@vibesys/run-session': null,
  '@vibesys/run-history': null,
  '@vibesys/transport': null,
  '@vibesys/hosts': null,
  '@vibesys/runs': null,
  '@vibesys/logs': null,
  '@vibesys/ipc': null,
  '@vibesys/connection': null,
  '@vibesys/subscription': null,
  // Its entries are node-only, so `runtime-reachability` already keeps it in `./main` entries.
  '@vibesys/settings': null,
};
/** An app imports the stylesheets of `@vibesys/ui` once, in its entry wiring. */
const STRICT_APP_STYLESHEETS = ['./tokens.css', './fonts.css', './components.css'];
/**
 * The third-party packages a feature or app imports and declares: no component kit, utility CSS or
 * icon package. Electron and Node builtins stay confined by `external-dependencies`.
 */
export const STRICT_THIRD_PARTY = ['react', 'react-dom', 'xstate'];
const STRICT_STYLE_FILE = /\.(?:css|scss|sass|less|styl|pcss)$/;
const STRICT_FIX =
  'build the UI from @vibesys/ui (add the missing component there) and drop the dependency';

/** The Biome plugins every feature and app source runs. */
export const STRICT_LINT_PLUGINS = [
  './tools/lint-rules/strict-no-raw-elements.grit',
  './tools/lint-rules/strict-no-raw-values.grit',
];
function strictErrors(root, workspacePackage, byName) {
  return [
    ...stylesheetErrors(root, workspacePackage),
    ...strictManifestErrors(workspacePackage, byName),
    ...strictImportErrors(root, workspacePackage, byName),
  ];
}

/** The design rules cover every feature and app package. */
function isDesignPackage({directory}) {
  return directory.startsWith('features/') || directory.startsWith('apps/');
}

function stylesheetErrors(root, {name, directory, manifest}) {
  const errors = [];
  const fix = 'style through the components and tokens of @vibesys/ui, and delete it';
  for (const [subpath, target] of Object.entries(manifest.exports ?? {})) {
    const source = sourceTarget(target) ?? '';
    if (STRICT_STYLE_FILE.test(subpath) || STRICT_STYLE_FILE.test(source)) {
      errors.push(
        violation(
          'strict-no-stylesheets',
          `${directory}/package.json`,
          `exports["${subpath}"] is a stylesheet (S2), but the strict package ${name} ships none; ${fix}`,
        ),
      );
    }
  }
  for (const path of filesWhere(join(root, directory), file => STRICT_STYLE_FILE.test(file))) {
    errors.push(
      violation(
        'strict-no-stylesheets',
        `${directory}/${path}`,
        `a stylesheet (S2) in the strict package ${name}; ${fix}`,
      ),
    );
  }
  return errors;
}

/** Whether `name` is a feature or app package: an app may compose feature screens. */
function isStrictPackage(name, byName) {
  const target = byName.get(name);
  return target !== undefined && isDesignPackage(target);
}

function strictManifestErrors({name, directory, manifest}, byName) {
  const errors = [];
  for (const dependency of Object.keys(manifest.dependencies ?? {})) {
    const message = isStrictPackage(dependency, byName)
      ? undefined
      : strictDependencyMessage(dependency);
    if (message === undefined) continue;
    errors.push(
      violation(
        'strict-imports',
        `${directory}/package.json`,
        `dependencies["${dependency}"] (S6) in the strict package ${name}: ${message}`,
      ),
    );
  }
  return errors;
}

/** Why a strict package may not depend on `dependency`, or undefined when it may. */
function strictDependencyMessage(dependency) {
  if (dependency.startsWith(SCOPE)) {
    if (Object.hasOwn(STRICT_WORKSPACE_IMPORTS, dependency)) return undefined;
    return `${dependency} is not a library a strict package may use; ${STRICT_FIX}`;
  }
  if (STRICT_THIRD_PARTY.includes(dependency) || dependency === 'electron') return undefined;
  return `${dependency} is outside ${STRICT_THIRD_PARTY.join(', ')}; ${STRICT_FIX}`;
}

function strictImportErrors(root, workspacePackage, byName) {
  const {directory} = workspacePackage;
  const packageRoot = join(root, directory);
  const errors = [];
  for (const path of sourceFiles(packageRoot).filter(file => !isTestCode(file))) {
    const text = readFileSync(join(packageRoot, path), 'utf8');
    for (const {specifier, line} of importedSpecifierLines(text)) {
      const message = strictSpecifierMessage(workspacePackage, specifier, byName);
      if (message !== undefined) {
        errors.push(violation('strict-imports', `${directory}/${path}:${line}`, message));
      }
    }
  }
  return errors;
}

/** Why a strict package may not import `specifier` (S6), or undefined when it may. */
function strictSpecifierMessage({name, manifest}, specifier, byName) {
  if (!specifier.startsWith(SCOPE)) return strictThirdPartyMessage(specifier);
  const target = splitSpecifier(specifier);
  if (target.name === name || !byName.has(target.name)) return undefined;
  if (isStrictPackage(target.name, byName)) return undefined;
  if (!Object.hasOwn(STRICT_WORKSPACE_IMPORTS, target.name)) {
    return `imports ${specifier}, which a strict package may not use (S6); ${STRICT_FIX}`;
  }
  const entries = STRICT_WORKSPACE_IMPORTS[target.name];
  const stylesheets = manifest.vibesys.layer === 'app' ? STRICT_APP_STYLESHEETS : [];
  if (entries === null || [...entries, ...stylesheets].includes(target.subpath)) return undefined;
  const allowed = entries.map(entry => `${target.name}${entry === '.' ? '' : entry.slice(1)}`);
  return (
    `imports ${specifier} (S6); a strict package imports only ${allowed.join(' and ')}: ` +
    'use the public components and tokens'
  );
}

function strictThirdPartyMessage(specifier) {
  if (externalPolicy(specifier)?.rules.includes(NODE_BUILTINS)) return undefined;
  const [first, second] = specifier.split('/');
  const base = first.startsWith('@') ? `${first}/${second}` : first;
  if (STRICT_THIRD_PARTY.includes(base) || base === 'electron') return undefined;
  return `imports ${specifier} (S6), outside ${STRICT_THIRD_PARTY.join(', ')}; ${STRICT_FIX}`;
}

function main() {
  const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
  const errors = packageGraphErrors(root);
  if (errors.length === 0) {
    console.log('The workspace package graph respects its layers, entries, and runtimes.');
    return 0;
  }
  console.error('Package graph violations (docs/contributing/clients-architecture.md):');
  for (const error of errors) console.error(`- ${error}`);
  return 1;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = main();
}
