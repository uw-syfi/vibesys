/**
 * Writes `src/tokens.css` from the token tables (`generate`), or fails when the checked-in file
 * differs from what they generate (`check`, rule S9, run by the `clients_quality` group). It runs under
 * bun, which imports the TypeScript tables directly; the logic is `renderTokensCss` and
 * `tokensCssDrift`, which this package's tests cover.
 *
 * This file is the package's I/O shell, so `biome.json` exempts `libs/ui/scripts/**` from the `ui/`
 * I/O rules. The alternatives are worse: `clients/tools/scripts/` may not import package code
 * (`scripts-do-not-import-package-code`), so the generator could not read the tables there; a
 * per-line lint suppression on every I/O import would hide the rule where it is meant to hold; a
 * package of its own for one script adds a node to the graph for no second user.
 */
import {readFileSync, writeFileSync} from 'node:fs';
import {renderTokensCss, tokensCssDrift} from '../src/tokens/css.js';

const source = (name: string): URL => new URL(`../src/${name}`, import.meta.url);
const primitives = readFileSync(source('primitives.css'), 'utf8');
const mode = process.argv[2];

if (mode === 'generate') {
  writeFileSync(source('tokens.css'), renderTokensCss(primitives));
} else if (mode === 'check') {
  const drift = tokensCssDrift(readFileSync(source('tokens.css'), 'utf8'), primitives);
  if (drift !== null) {
    console.error(drift);
    process.exit(1);
  }
} else {
  console.error('usage: bun scripts/tokens-css.ts generate|check');
  process.exit(2);
}
