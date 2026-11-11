/**
 * A grouped control (segments, tiles) draws its selection from `data-selected`, which its component
 * sets from the owner's value; the radio's own `:checked` state never draws it, and a hover is only
 * a tint of the ground, so a hovered option never looks chosen.
 */
import {describe, expect, test} from 'bun:test';
import {readdirSync, readFileSync} from 'node:fs';

const sheets = readdirSync(new URL('.', import.meta.url))
  .filter(name => name.endsWith('.css'))
  .map(name => [name, readFileSync(new URL(name, import.meta.url), 'utf8')] as const);

/** Every rule as [selector list, declarations]. */
const rulesOf = (source: string): readonly (readonly [string, string])[] =>
  [...source.matchAll(/(^|\n)([^\n{}/@][^{}]*)\{([^}]*)\}/g)].map(
    found => [(found[2] ?? '').trim(), found[3] ?? ''] as const,
  );

const GROUPED = ['.vs-segment', '.vs-choice-tile'] as const;

describe('the selection of a grouped control', () => {
  test('is never drawn from :checked inside a :has', () => {
    for (const [name, source] of sheets) {
      expect({name, found: /:has\([^)]*:checked/.test(source)}).toEqual({name, found: false});
    }
  });

  test('is drawn from data-selected for every grouped control', () => {
    for (const group of GROUPED) {
      const drawn = sheets
        .flatMap(([, source]) => rulesOf(source))
        .filter(([selector]) => selector.includes(`${group}[data-selected]`));
      expect(drawn.length, group).toBeGreaterThan(0);
    }
  });

  test('is never the look of a hover: a hover rule sets only a background', () => {
    for (const [name, source] of sheets) {
      for (const [selector, body] of rulesOf(source)) {
        if (!GROUPED.some(group => selector.includes(group)) || !selector.includes(':hover')) {
          continue;
        }
        const props = [...body.matchAll(/(^|[;{\s])([a-z-]+):/g)].map(found => found[2]);
        expect({name, selector, props}).toEqual({name, selector, props: ['background']});
      }
    }
  });
});
