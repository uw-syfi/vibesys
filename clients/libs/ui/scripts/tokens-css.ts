/**
 * Writes `src/tokens.css` from the token tables (`generate`), or fails when the checked-in file
 * differs from what they generate (`check`, rule S9, run by the `tui_quality` group). It runs under
 * bun, which imports the TypeScript tables directly; the logic is `renderTokensCss` and
 * `tokensCssDrift`, which this package's tests cover. This is the package's I/O shell, not
 * presentational code, so `biome.json` exempts `scripts/` from the `ui/` I/O rules.
 */
import {readFileSync, writeFileSync} from 'node:fs';
import {renderTokensCss, tokensCssDrift} from '../src/tokens/index.js';

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
