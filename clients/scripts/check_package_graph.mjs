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
 *   `./main` (node).
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
 * - `external-dependencies`: React only in `dom` entries, Electron only in `node` entries of
 *   features and apps, Node builtins only in `node` entries (`EXTERNAL_DEPENDENCIES`); a package
 *   that declares one of them in `dependencies` has an entry that may use it.
 * - `entry-projects`: each entry of a package on the per-entry layout is compiled by its own
 *   composite `tsconfig.<entry>.json` (`tsconfig.lib.json` for `.`).
 * - `entry-capability`: that project's `lib` and `types` are its runtime's: no DOM and no Node
 *   types for `neutral`, DOM without Node types for `dom`, Node types without DOM for `node`. The
 *   compiler then rejects a Node import in a renderer entry.
 * - `references-match-dependencies`: the package's tsconfig projects reference a project of every
 *   workspace dependency and of no other workspace package. A missing reference is not a compiler
 *   error (the import resolves to the dependency's source), so this is where it is caught.
 * - `domain-free`: the production source of a domain-free package (`DOMAIN_FREE`) names no word of
 *   the product's domain (`DOMAIN_WORDS`), in identifiers, strings, or comments, so a primitive
 *   that grows a domain concept is caught where the layer rule alone cannot see it.
 * - `bucket-names`: a foundation, core, or domain package is named for the one concept it owns, so
 *   no segment of its name is a bucket word (`BUCKET_NAMES`: entities, core, common, ...). A bucket
 *   name invites unrelated code in, which the layer rule cannot see.
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
  'entry-projects',
  'entry-capability',
  'references-match-dependencies',
  'domain-free',
  'bucket-names',
];

/** Layer ranks. A package may depend only on a strictly lower rank. */
export const LAYERS = {foundation: 0, core: 1, domain: 2, service: 3, feature: 4, app: 5};

/** What each runtime's compiler program may see. */
export const RUNTIMES = {
  neutral: {dom: false, node: false},
  dom: {dom: true, node: false},
  node: {dom: false, node: true},
};

/** A feature's possible entries and their fixed runtimes. */
export const FEATURE_ENTRIES = {'./state': 'neutral', './renderer': 'dom', './main': 'node'};

/**
 * Third-party imports confined by runtime and layer, the one allowlist for them. A specifier
 * matches an entry when it is the package or one of its subpaths. An importing entry must have one
 * of `runtimes`, and its package one of `layers` when that is given. Node builtins (`node:*` and
 * the bare names in `builtinModules`) are confined to `node` entries the same way.
 */
export const EXTERNAL_DEPENDENCIES = [
  {packages: ['react', 'react-dom'], runtimes: ['dom']},
  {packages: ['electron'], runtimes: ['node'], layers: ['feature', 'app']},
  {packages: ['@opentui/core'], runtimes: ['node'], layers: ['app']},
];
const NODE_BUILTINS = {runtimes: ['node']};

/**
 * Packages that predate the per-entry layout, with the rules they skip until the item named in
 * the migration table moves them. The per-entry project rules (`entry-projects` stands for all
 * three) cannot apply to their single tsconfig per package. Every other rule applies. This table
 * only shrinks.
 */
export const LEGACY_EXEMPTIONS = {
  '@vibesys/desktop': new Set(['entry-projects']),
  '@vibesys/tui': new Set(['entry-projects']),
};

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
    if (key !== 'layer' && key !== 'entries') {
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
  for (const [subpath, runtime] of Object.entries(entries)) {
    const expected = FEATURE_ENTRIES[subpath];
    if (expected === undefined) {
      const allowed = Object.keys(FEATURE_ENTRIES).join(', ');
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

/** Source files under the package, package-relative, skipping installs, output, and dot-dirs. */
function sourceFiles(packageRoot, relativeDirectory = '') {
  const files = [];
  for (const entry of readdirSync(join(packageRoot, relativeDirectory), {withFileTypes: true})) {
    if (entry.name.startsWith('.') || SKIPPED_DIRECTORIES.has(entry.name)) continue;
    const path = relativeDirectory === '' ? entry.name : `${relativeDirectory}/${entry.name}`;
    if (entry.isDirectory()) files.push(...sourceFiles(packageRoot, path));
    else if (SOURCE_FILE.test(entry.name) && !DECLARATION_FILE.test(entry.name)) files.push(path);
  }
  return files;
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

/** The `@vibesys/*` specifiers among them. */
export function workspaceSpecifiers(text) {
  return importedSpecifiers(text).filter(name => name.startsWith(SCOPE));
}

/** The confinement for a third-party specifier, or undefined when it is unconfined. */
export function externalPolicy(specifier) {
  if (specifier.startsWith('node:') || builtinModules.includes(specifier.split('/')[0])) {
    return {
      name: specifier.startsWith('node:') ? specifier : `node:${specifier}`,
      ...NODE_BUILTINS,
    };
  }
  const policy = EXTERNAL_DEPENDENCIES.find(({packages}) =>
    packages.some(name => specifier === name || specifier.startsWith(`${name}/`)),
  );
  return policy === undefined ? undefined : {name: specifier, ...policy};
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
  if (!policy.runtimes.includes(entry.runtime)) return false;
  return (
    policy.layers === undefined || policy.layers.includes(workspacePackage.manifest.vibesys.layer)
  );
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
  }
  return errors;
}

function externalError(workspacePackage, path, specifier) {
  const policy = externalPolicy(specifier);
  if (policy === undefined || isTestCode(path)) return undefined;
  const importer = owningEntry(packageEntries(workspacePackage), path);
  if (importer === undefined || policyAllows(policy, workspacePackage, importer)) return undefined;
  const layers = policy.layers === undefined ? '' : ` of a ${policy.layers.join(' or ')}`;
  return violation(
    'external-dependencies',
    `${workspacePackage.directory}/${path}`,
    `the ${importer.runtime} entry ${workspacePackage.name} ${importer.subpath} must not import ` +
      `${specifier}; only ${policy.runtimes.join(' or ')} entries${layers} may`,
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

function main() {
  const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
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
