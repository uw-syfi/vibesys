#!/usr/bin/env node

/**
 * Rule F1 of the desktop design system (docs/contributing/clients-architecture.md, "Design system
 * enforcement"): the old UI packages get fixes only. A change compared with its merge base may,
 * in a package listed in `FROZEN_UI_PACKAGES`:
 *
 * - add no file under the package's UI paths (test files are fixes' companions and are allowed);
 * - add no name to the exports of its `./renderer` entry;
 * - raise no file's count of JSX intrinsic interactive elements (`INTERACTIVE_ELEMENTS`) or of raw
 *   color literals, so a fix can move or delete such code but not add more of it.
 *
 * A listed package that no longer exists is an error too, so the list only shrinks. A package
 * leaves it when it is deleted.
 *
 * The check is a function of two file trees, each a map from a path relative to the workspace root
 * (`features/campaign/src/renderer/x.tsx`) to the file's text, holding the files of the frozen
 * packages: `package.json`, and the `.ts`, `.tsx` and `.css` files. `frozenUiErrors` takes them
 * and knows nothing of Git; the command line builds the trees: the base from `--base-dir`, or from
 * `--base-ref` through its merge base with HEAD (fetching the ref when the checkout is shallow),
 * the head from `--head-dir`, by default the working tree.
 */

import {spawnSync} from 'node:child_process';
import {existsSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {dirname, join, posix, relative, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import ts from 'typescript';

const FEATURE_UI = ['src/renderer/'];

/**
 * The frozen packages, by directory under `clients/`, each with the paths (a directory ending in
 * `/`, or a file) that hold its UI. This list is the one source for the frozen set and only
 * shrinks.
 */
export const FROZEN_UI_PACKAGES = {
  'features/campaign': FEATURE_UI,
  'features/run-view': FEATURE_UI,
  'features/start-run': FEATURE_UI,
  'features/hosts': FEATURE_UI,
  'features/recent-runs': FEATURE_UI,
  'features/run-control': FEATURE_UI,
  'features/conversation': FEATURE_UI,
  'features/gateway-connect': FEATURE_UI,
  'features/replay': FEATURE_UI,
  'features/web-theme': FEATURE_UI,
  'apps/web': ['src/app/', 'src/styles.css'],
  // The desktop renderer's welcome view; the redesigned shell is composed elsewhere in the app.
  'apps/desktop-renderer': ['src/ui/', 'src/welcome.css'],
};

/** JSX intrinsic elements a person operates: the raw controls `@vibesys/ui` wraps. */
export const INTERACTIVE_ELEMENTS = new Set([
  'button',
  'a',
  'input',
  'select',
  'textarea',
  'label',
  'dialog',
  'details',
  'summary',
  'form',
]);

/** A hex color of 3, 4, 6 or 8 digits, or a color function. */
const COLOR_LITERAL =
  /(?<![\w&])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})\b|\b(?:rgba?|hsla?|oklch|color-mix)\(/g;

const COUNTED_FILE = /\.(?:tsx?|css)$/;
const TEST_FILE = /\.test\.tsx?$/;
const RENDERER_ENTRY = './renderer';
const SKIPPED_DIRECTORIES = new Set(['node_modules', 'dist']);
const FIX = 'build the screen in a strict package from @vibesys/ui, or fix the old code in place';

function violation(where, message) {
  return `frozen-ui: ${where}: ${message}`;
}

/** How many JSX intrinsic interactive elements `text` renders; only `.tsx` files render JSX. */
export function interactiveElementCount(text, path) {
  if (!path.endsWith('.tsx')) return 0;
  const file = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let count = 0;
  const visit = node => {
    const isElement = ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node);
    if (isElement && ts.isIdentifier(node.tagName) && INTERACTIVE_ELEMENTS.has(node.tagName.text)) {
      count += 1;
    }
    ts.forEachChild(node, visit);
  };
  visit(file);
  return count;
}

/** How many raw color literals `text` holds. */
export function colorLiteralCount(text) {
  return (text.match(COLOR_LITERAL) ?? []).length;
}

function inUiPaths(directory, uiPaths, path) {
  const inside = path.slice(directory.length + 1);
  return path.startsWith(`${directory}/`) && uiPaths.some(prefix => inside.startsWith(prefix));
}

// --- exports of the renderer entry ------------------------------------------------------------

function rendererEntrySource(tree, directory) {
  const manifest = tree.get(`${directory}/package.json`);
  if (manifest === undefined) return undefined;
  const source = JSON.parse(manifest).exports?.[RENDERER_ENTRY]?.['@vibesys/source'];
  return typeof source === 'string' ? posix.join(directory, source) : undefined;
}

function resolveModule(tree, from, specifier) {
  const base = posix.join(posix.dirname(from), specifier.replace(/\.[cm]?js$/, ''));
  return ['.ts', '.tsx', '/index.ts', '/index.tsx']
    .map(suffix => `${base}${suffix}`)
    .find(candidate => tree.has(candidate));
}

function declaredNames(statement) {
  if (ts.isVariableStatement(statement)) {
    return statement.declarationList.declarations
      .map(declaration => declaration.name)
      .filter(ts.isIdentifier)
      .map(name => name.text);
  }
  return statement.name !== undefined && ts.isIdentifier(statement.name)
    ? [statement.name.text]
    : [];
}

function statementExports(tree, path, statement, visited) {
  if (ts.isExportAssignment(statement)) return ['default'];
  if (ts.isExportDeclaration(statement)) {
    const clause = statement.exportClause;
    if (clause !== undefined) {
      return ts.isNamedExports(clause)
        ? clause.elements.map(each => each.name.text)
        : [clause.name.text];
    }
    const target =
      statement.moduleSpecifier && resolveModule(tree, path, statement.moduleSpecifier.text);
    return target === undefined ? [] : exportedNames(tree, target, visited);
  }
  const exported = ts.getModifiers(statement)?.some(m => m.kind === ts.SyntaxKind.ExportKeyword);
  if (!exported) return [];
  const isDefault = ts
    .getModifiers(statement)
    ?.some(modifier => modifier.kind === ts.SyntaxKind.DefaultKeyword);
  return isDefault ? ['default'] : declaredNames(statement);
}

/** The names a module exports, following `export *` into modules of the tree. */
export function exportedNames(tree, path, visited = new Set()) {
  if (visited.has(path) || !tree.has(path)) return [];
  visited.add(path);
  const kind = path.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const file = ts.createSourceFile(path, tree.get(path), ts.ScriptTarget.Latest, true, kind);
  return [...new Set(file.statements.flatMap(each => statementExports(tree, path, each, visited)))];
}

function exportErrors(base, head, directory) {
  const entry = rendererEntrySource(head, directory);
  if (entry === undefined) return [];
  const before = new Set(exportedNames(base, rendererEntrySource(base, directory) ?? entry));
  return exportedNames(head, entry)
    .filter(name => !before.has(name))
    .map(name =>
      violation(
        `${directory}/package.json`,
        `exports["${RENDERER_ENTRY}"] gains \`${name}\` (F1); ${directory} is frozen, so ${FIX}`,
      ),
    );
}

// --- files ------------------------------------------------------------------------------------

function fileErrors(base, head, directory, uiPaths) {
  const errors = [];
  for (const [path, text] of head) {
    if (!path.startsWith(`${directory}/`) || !COUNTED_FILE.test(path) || TEST_FILE.test(path)) {
      continue;
    }
    if (!inUiPaths(directory, uiPaths, path)) continue;
    if (!base.has(path)) {
      errors.push(violation(path, `a new file in the frozen UI of ${directory} (F1); ${FIX}`));
      continue;
    }
    errors.push(...growthErrors(path, base.get(path), text));
  }
  return errors;
}

function growthErrors(path, before, after) {
  const measures = [
    [
      'JSX interactive elements',
      'use `<Button>`, `<Field>` and the rest of @vibesys/ui',
      interactiveElementCount,
    ],
    ['raw color literals', 'use a token from @vibesys/ui/tokens', colorLiteralCount],
  ];
  return measures.flatMap(([what, fix, count]) => {
    const [was, now] = [count(before, path), count(after, path)];
    if (now <= was) return [];
    return [
      violation(
        path,
        `raises its count of ${what} from ${was} to ${now} (F1); the old UI gets fixes only, so ` +
          `remove it, or ${fix} in a strict package`,
      ),
    ];
  });
}

/**
 * Every F1 violation of `head` against `base`.
 *
 * @param {Map<string, string>} base the merge base's files of the frozen packages
 * @param {Map<string, string>} head the changed tree's
 * @returns {string[]} one `frozen-ui: <path>: <message>` line per violation
 */
export function frozenUiErrors(base, head) {
  const errors = [];
  for (const [directory, uiPaths] of Object.entries(FROZEN_UI_PACKAGES)) {
    if (!head.has(`${directory}/package.json`)) {
      errors.push(
        violation(
          'FROZEN_UI_PACKAGES',
          `lists ${directory}, which no longer exists; delete the entry (the list only shrinks)`,
        ),
      );
      continue;
    }
    errors.push(...fileErrors(base, head, directory, uiPaths));
    errors.push(...exportErrors(base, head, directory));
  }
  return errors;
}

// --- reading trees ----------------------------------------------------------------------------

function walk(root, relativeDirectory, files) {
  for (const entry of readdirSync(join(root, relativeDirectory), {withFileTypes: true})) {
    if (entry.name.startsWith('.') || SKIPPED_DIRECTORIES.has(entry.name)) continue;
    const path = posix.join(relativeDirectory, entry.name);
    if (entry.isDirectory()) walk(root, path, files);
    else if (COUNTED_FILE.test(entry.name) || entry.name === 'package.json') files.push(path);
  }
}

/** The frozen packages' files under the workspace at `root`, as a tree. */
export function readTree(root) {
  const tree = new Map();
  for (const directory of Object.keys(FROZEN_UI_PACKAGES)) {
    if (!existsSync(join(root, directory))) continue;
    const files = [];
    walk(root, directory, files);
    for (const path of files) tree.set(path, readFileSync(join(root, path), 'utf8'));
  }
  return tree;
}

function git(root, ...args) {
  const result = spawnSync('git', ['-C', root, ...args], {encoding: 'utf8'});
  if (result.status !== 0) {
    throw new Error(`git ${args.join(' ')} failed: ${result.stderr.trim()}`);
  }
  return result.stdout;
}

/** Fetch the base branch's history when the checkout is shallow or lacks it, as CI's is. */
function ensureBase(root, baseRef) {
  const shallow = git(root, 'rev-parse', '--is-shallow-repository').trim() === 'true';
  const known =
    spawnSync('git', ['-C', root, 'rev-parse', '--verify', baseRef], {encoding: 'utf8'}).status ===
    0;
  if (!baseRef.startsWith('origin/') || (known && !shallow)) return;
  const branch = baseRef.slice('origin/'.length);
  git(
    root,
    'fetch',
    '--no-tags',
    ...(shallow ? ['--unshallow'] : []),
    'origin',
    `+refs/heads/${branch}:refs/remotes/origin/${branch}`,
  );
}

/** The frozen packages' files at the merge base of HEAD and `baseRef`, extracted to `into`. */
function extractBase(workspaceRoot, baseRef, into) {
  const top = git(workspaceRoot, 'rev-parse', '--show-toplevel').trim();
  ensureBase(top, baseRef);
  const base = git(top, 'merge-base', 'HEAD', baseRef).trim();
  const prefix = relative(realpathSync(top), realpathSync(workspaceRoot));
  const wanted = Object.keys(FROZEN_UI_PACKAGES).map(directory => join(prefix, directory));
  // A package the base lacks has no files there: every UI file of it is new.
  const present = git(top, 'ls-tree', '--name-only', base, '--', ...wanted)
    .split('\n')
    .filter(Boolean);
  const archive = spawnSync('git', ['-C', top, 'archive', '--format=tar', base, '--', ...present], {
    maxBuffer: 512 * 1024 * 1024,
  });
  if (archive.status !== 0) throw new Error(`git archive failed: ${archive.stderr}`);
  const untar = spawnSync('tar', ['-x', '-C', into], {input: archive.stdout});
  if (untar.status !== 0) throw new Error(`tar failed: ${untar.stderr}`);
  return join(into, prefix);
}

function option(name) {
  const index = process.argv.indexOf(name);
  return index === -1 ? undefined : process.argv[index + 1];
}

function main() {
  const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
  const baseDirectory = option('--base-dir');
  const baseRef = option('--base-ref');
  if ((baseDirectory === undefined) === (baseRef === undefined)) {
    console.error(
      'usage: check_frozen_ui.mjs (--base-ref <ref> | --base-dir <workspace root>) [--head-dir <workspace root>]',
    );
    return 2;
  }
  const scratch = mkdtempSync(join(tmpdir(), 'vibesys-frozen-ui-'));
  try {
    const baseRoot = baseDirectory ?? extractBase(root, baseRef, scratch);
    const errors = frozenUiErrors(
      readTree(resolve(baseRoot)),
      readTree(resolve(option('--head-dir') ?? root)),
    );
    if (errors.length === 0) {
      console.log('The frozen UI packages gained no UI (F1).');
      return 0;
    }
    console.error('Frozen UI violations (docs/contributing/clients-architecture.md):');
    for (const error of errors) console.error(`- ${error}`);
    return 1;
  } finally {
    rmSync(scratch, {recursive: true, force: true});
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = main();
}
