/**
 * The motion rule of the design system, held by reading every stylesheet and source of the client
 * workspace: content moves away from the edge it is anchored to and never vertically, and what
 * enters moves a step, not a distance. A keyframe that translates along Y fails, and so does any
 * entrance step of a screen or a row beyond `--space-2`; a pane that enters from the right edge has
 * its own named, larger step. The allowlists are expected to stay empty.
 */

import {describe, expect, test} from 'bun:test';
import {readdirSync, readFileSync} from 'node:fs';
import {join} from 'node:path';
import {SPACE} from './tokens/index.js';

/** Keyframes allowed to translate along Y, each with its reason. Expected empty. */
const VERTICAL_ALLOWED: Readonly<Record<string, string>> = {};

/** Keyframes with a larger horizontal step, each with its reason: an edge entrance, or a sweep inside its own track. */
const EDGE_ENTRANCES: Readonly<Record<string, string>> = {
  'vs-pane-enter': 'a pane is anchored to the right edge and enters from it',
  'vs-meter-sweep': 'an indeterminate bar sweeps along its own track; it is not an entrance',
};

const WORKSPACE = new URL('../../../', import.meta.url).pathname;
const SKIPPED = new Set(['node_modules', 'dist', 'out', 'build', '.turbo']);

function sources(directory: string, pattern: RegExp): string[] {
  return readdirSync(directory, {withFileTypes: true}).flatMap(entry => {
    if (SKIPPED.has(entry.name)) return [];
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return sources(path, pattern);
    return pattern.test(entry.name) ? [path] : [];
  });
}

interface Keyframes {
  readonly file: string;
  readonly name: string;
  readonly body: string;
}

function keyframesOf(file: string): Keyframes[] {
  const text = readFileSync(file, 'utf8');
  const found: Keyframes[] = [];
  for (const match of text.matchAll(/@keyframes\s+([\w-]+)\s*\{/g)) {
    let depth = 1;
    let at = (match.index ?? 0) + match[0].length;
    const start = at;
    while (depth > 0 && at < text.length) {
      if (text[at] === '{') depth += 1;
      if (text[at] === '}') depth -= 1;
      at += 1;
    }
    found.push({file, name: match[1] ?? '', body: text.slice(start, at - 1)});
  }
  return found;
}

const stylesheets = sources(WORKSPACE, /\.css$/);
const keyframes = stylesheets.flatMap(keyframesOf);

/** The translate functions of a keyframe's body, as their argument lists. */
const translations = (body: string): {fn: string; args: string}[] =>
  [...body.matchAll(/(translate[XYZ3d]*)\(([^;]*?)\)\s*(?:;|$|\n)/g)].map(match => ({
    fn: match[1] ?? '',
    args: match[2] ?? '',
  }));

/** Whether a translate moves along Y (or Z, which reads as depth, not as an entrance). */
function movesVertically(fn: string, args: string): boolean {
  if (fn === 'translateY' || fn === 'translateZ' || fn === 'translate3d') return true;
  if (fn === 'translate') return args.includes(',') && !/,\s*0(px)?\s*$/.test(args);
  return false;
}

/** The pixels of a step written `var(--space-N)`, optionally `calc(var(--space-N) * k)`. */
function stepPixels(args: string): number {
  const token = /var\(--(space-\d)\)/.exec(args)?.[1] as keyof typeof SPACE | undefined;
  if (token === undefined) return Number.POSITIVE_INFINITY;
  const factor = Number(/\*\s*(-?[\d.]+)/.exec(args)?.[1] ?? 1);
  return Math.abs(SPACE[token] * factor);
}

describe('motion', () => {
  test('finds the keyframes it is to read', () => {
    const names = keyframes.map(each => each.name);
    expect(names).toContain('vs-arrive');
    expect(names).toContain('vs-enter');
    expect(names).toContain('vs-pane-enter');
  });

  test('no keyframe translates along Y, unless it is named and given its reason', () => {
    const vertical = keyframes
      .filter(each => translations(each.body).some(t => movesVertically(t.fn, t.args)))
      .map(each => each.name)
      .filter(name => VERTICAL_ALLOWED[name] === undefined);
    expect(vertical).toEqual([]);
    for (const reason of Object.values(VERTICAL_ALLOWED)) expect(reason.length).toBeGreaterThan(0);
  });

  test('a screen or a row enters by at most one --space-2, and only a named edge entrance by more', () => {
    const limit = SPACE['space-2'];
    const tooFar = keyframes
      .filter(each => EDGE_ENTRANCES[each.name] === undefined)
      .flatMap(each =>
        translations(each.body).map(t => ({name: each.name, px: stepPixels(t.args)})),
      )
      .filter(step => step.px > limit)
      .map(step => step.name);
    expect(tooFar).toEqual([]);
  });

  test('every named edge entrance is still a keyframe, so the list does not rot', () => {
    for (const name of Object.keys(EDGE_ENTRANCES)) {
      expect(keyframes.map(each => each.name)).toContain(name);
    }
    for (const name of Object.keys(VERTICAL_ALLOWED)) {
      expect(keyframes.map(each => each.name)).toContain(name);
    }
  });

  test('no source animates a vertical entrance outside a stylesheet', () => {
    const scripts = sources(WORKSPACE, /\.(tsx?|mjs)$/).filter(
      file => !/\.(test|spec)\.|\.d\.ts$|motion\.test\.ts$/.test(file),
    );
    const offenders = scripts.filter(file =>
      /translateY\(|translate3d\(|\.animate\(\s*[[{]/.test(readFileSync(file, 'utf8')),
    );
    expect(offenders).toEqual([]);
  });
});
