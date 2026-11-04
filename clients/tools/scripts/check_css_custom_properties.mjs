#!/usr/bin/env node

/**
 * Every `var(--name)` without a fallback must name a custom property something defines.
 *
 * A reference to a deleted token builds, type checks and merges cleanly, then makes the property
 * that uses it invalid at runtime (`top: calc(var(--titlebar) + ...)` stopped positioning a toast
 * stack when the token went away in a concurrent change). Nothing else reads CSS against the
 * tokens, so this does: one pass over every stylesheet and TypeScript source in the workspace.
 *
 * Definitions are names the scan can see being set: a `--name:` declaration or `@property --name`
 * in a stylesheet, and a `'--name':` style-object key or `setProperty('--name', ...)` in a
 * TypeScript source. References are `var(--name)` in those same files. A reference with a fallback
 * (`var(--name, 0)`) always resolves, so it passes. A name built at run time (`--series-${n}`) cannot
 * be found statically: a definition built that way is invisible, and a reference built that way is
 * not checked, so a `var()` on a name set that way needs a fallback to pass.
 *
 * The scan is by name, not by scope: a property set on one element satisfies a reference anywhere.
 * That is the failure this closes (a name nothing sets), not a proof that every use sees a value.
 * Biome has no rule for this, and stylelint's `no-unknown-custom-properties` could not read
 * definitions that TypeScript sets, so it would need the same script plus a dependency.
 */
import {readdirSync, readFileSync} from 'node:fs';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {workspaceLayout} from './workspace_layout.mjs';

const STYLESHEET = /\.css$/;
const SCRIPT = /\.(?:[cm]?ts|tsx)$/;
// Tests assert on strings and set properties to probe components; neither is shipped styling.
const NOT_SHIPPED = /\.(?:test|spec)\.[cm]?tsx?$|\.d\.[cm]?ts$/;
const SKIPPED_DIRECTORIES = new Set(['node_modules', 'dist']);

const NAME = String.raw`--[A-Za-z_][\w-]*`;
// `var(--name)` and `var(--name, fallback)`; a name cut short by `${` never reaches `,` or `)`.
const REFERENCE = new RegExp(String.raw`var\(\s*(${NAME})\s*([,)])`, 'g');
const STYLESHEET_DEFINITIONS = [
  new RegExp(String.raw`(?<![\w-])(${NAME})\s*:`, 'g'),
  new RegExp(String.raw`@property\s+(${NAME})`, 'g'),
];
const SCRIPT_DEFINITIONS = [
  new RegExp(String.raw`(['"\`])(${NAME})\1\s*:`, 'g'),
  new RegExp(String.raw`setProperty\(\s*(['"\`])(${NAME})\1`, 'g'),
];

/**
 * @typedef {{path: string, text: string}} SourceFile
 * @typedef {{name: string, line: number, hasFallback: boolean}} Reference
 */

/** Replaces each block comment with spaces, keeping newlines so line numbers still match. */
function withoutStylesheetComments(text) {
  return text.replaceAll(/\/\*[\s\S]*?\*\//g, comment => comment.replaceAll(/[^\n]/g, ' '));
}

function lineAt(text, index) {
  let line = 1;
  for (let at = text.indexOf('\n'); at !== -1 && at < index; at = text.indexOf('\n', at + 1)) {
    line += 1;
  }
  return line;
}

/** The custom properties a file sets, by the patterns its kind of file sets them with. */
function definitionsIn({path, text}) {
  if (STYLESHEET.test(path)) {
    const source = withoutStylesheetComments(text);
    return STYLESHEET_DEFINITIONS.flatMap(pattern =>
      [...source.matchAll(pattern)].map(match => match[1]),
    );
  }
  return SCRIPT_DEFINITIONS.flatMap(pattern => [...text.matchAll(pattern)].map(match => match[2]));
}

/** @returns {Reference[]} */
function referencesIn({path, text}) {
  const source = STYLESHEET.test(path) ? withoutStylesheetComments(text) : text;
  return [...source.matchAll(REFERENCE)].map(match => ({
    name: match[1],
    line: lineAt(source, match.index),
    hasFallback: match[2] === ',',
  }));
}

/**
 * The references no file defines and that have no fallback, one message per reference.
 *
 * @param {SourceFile[]} files stylesheets and TypeScript sources, with workspace-relative paths
 * @returns {string[]} messages naming the file, the line and the undefined property
 */
export function undefinedCustomPropertyErrors(files) {
  const shipped = files.filter(
    file => (STYLESHEET.test(file.path) || SCRIPT.test(file.path)) && !NOT_SHIPPED.test(file.path),
  );
  const defined = new Set(shipped.flatMap(definitionsIn));
  return shipped.flatMap(file =>
    referencesIn(file)
      .filter(({name, hasFallback}) => !hasFallback && !defined.has(name))
      .map(
        ({name, line}) =>
          `css-custom-property: ${file.path}:${line}: var(${name}) names a custom property that no ` +
          `stylesheet or TypeScript style defines, so the declaration using it is invalid at ` +
          `runtime. Define ${name}, point the reference at a token that exists, or give it a ` +
          `fallback, var(${name}, <value>); a property set at run time under a computed name ` +
          `cannot be found here and needs the fallback`,
      ),
  );
}

/** Every stylesheet and TypeScript source under the workspace's scan roots. */
export function readWorkspaceSources(root) {
  const files = [];
  const walk = directory => {
    for (const entry of readdirSync(join(root, directory), {withFileTypes: true})) {
      const path = `${directory}/${entry.name}`;
      if (entry.isDirectory()) {
        if (!SKIPPED_DIRECTORIES.has(entry.name) && !entry.name.startsWith('.')) walk(path);
      } else if (STYLESHEET.test(entry.name) || SCRIPT.test(entry.name)) {
        files.push({path, text: readFileSync(join(root, path), 'utf8')});
      }
    }
  };
  for (const scanRoot of workspaceLayout(root).scanRoots) walk(scanRoot);
  return files;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
  const errors = undefinedCustomPropertyErrors(readWorkspaceSources(root));
  for (const error of errors) console.error(error);
  process.exitCode = errors.length > 0 ? 1 : 0;
}
