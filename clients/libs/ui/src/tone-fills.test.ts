/**
 * The app draws no banner: no strip washed with a status color. A rule that paints a background
 * with a tone fill (`--chip-*-bg`, `--banner-*-bg`, or a mix of a status color) must be on the
 * list below, whatever its selector is called; the list is what may be tinted, each with the
 * reason it is not a strip. A new selector fails here until it is argued in review.
 * `e2e/checks/in-page.ts` (`bannerCheck`) holds the other half: the colors a screen is drawn in.
 */

import {describe, expect, test} from 'bun:test';
import {readdirSync, readFileSync} from 'node:fs';

/** What may be washed with a tone fill, by selector (an `@keyframes` step by `@keyframes name > step`). */
const MAY_TINT: Readonly<Record<string, string>> = {
  '.vs-chip[data-tone="live"]': 'a chip is as wide as its label',
  '.vs-chip[data-tone="ok"]': 'a chip is as wide as its label',
  '.vs-chip[data-tone="warn"]': 'a chip is as wide as its label',
  '.vs-chip[data-tone="err"]': 'a chip is as wide as its label',
  '.vs-toast[data-tone="warn"]': 'a toast is transient and sits top right',
  '.vs-toast[data-tone="err"]': 'a toast is transient and sits top right',
  '.vs-overview-tile[data-attention]': 'an attention tile is one cell of a row of tiles',
  '.vs-new-below': 'a pill as wide as its label',
  '.vs-button[data-variant="danger"]:hover': 'the hover state of a button',
  '@keyframes vs-pane-flash > from': 'a flash of a pane on arrival, gone in a moment',
};

/** A background value that washes with a tone: a tone fill token, or a mix of a status color. */
const WASH = /var\(--(?:banner|chip)-[a-z]+-bg\)|color-mix\([^;]*var\(--(?:accent|ok|warn|err)\)/;

interface Rule {
  readonly selector: string;
  readonly body: string;
}

/** The index just past the `}` that closes the `{` at `open`. */
function closeOf(text: string, open: number): number {
  let depth = 0;
  for (let at = open; at < text.length; at += 1) {
    if (text[at] === '{') depth += 1;
    if (text[at] === '}') depth -= 1;
    if (depth === 0) return at + 1;
  }
  return text.length;
}

/** The rules of `css`, comments dropped; a rule inside `@keyframes name` is named `@keyframes name > step`. */
function rulesOf(css: string): Rule[] {
  const walk = (text: string, scope: string): Rule[] => {
    const open = text.indexOf('{');
    if (open === -1) return [];
    const close = closeOf(text, open);
    const prelude = text.slice(0, open).trim().replace(/\s+/g, ' ');
    const inner = text.slice(open + 1, close - 1);
    return [...blockRules(prelude, inner, scope, walk), ...walk(text.slice(close), scope)];
  };
  return walk(css.replace(/\/\*[\s\S]*?\*\//g, ''), '');
}

/** The rules of one block: a rule's selectors, or the rules an at-rule holds. */
function blockRules(
  prelude: string,
  inner: string,
  scope: string,
  walk: (text: string, scope: string) => Rule[],
): Rule[] {
  if (prelude.startsWith('@keyframes')) return walk(inner, prelude);
  if (prelude.startsWith('@')) return walk(inner, scope);
  return prelude.split(',').map(selector => ({
    selector: `${scope === '' ? '' : `${scope} > `}${selector.trim()}`,
    body: inner,
  }));
}

/** The selectors in `css` that wash a background with a tone and are not on the list. */
function tinting(css: string): string[] {
  return rulesOf(css)
    .filter(rule =>
      rule.body
        .split(';')
        .some(
          declaration =>
            /^\s*background(?:-color|-image)?\s*:/.test(declaration) && WASH.test(declaration),
        ),
    )
    .map(rule => rule.selector)
    .filter(selector => !(selector in MAY_TINT));
}

describe('the tone fills', () => {
  test('refuse a panel that tints itself, under any name and by any spelling', () => {
    expect(tinting('.vs-notice { background: var(--banner-warn-bg); }')).toEqual(['.vs-notice']);
    // The mutation that passed the old guard: a strip with the chip fill.
    expect(
      tinting(
        '.vs-notice { display: flex; border: 1px solid var(--chip-warn-border); background: var(--chip-warn-bg); }',
      ),
    ).toEqual(['.vs-notice']);
    expect(
      tinting('.strip { background-color: color-mix(in srgb, var(--warn) 9%, var(--bg)); }'),
    ).toEqual(['.strip']);
    expect(tinting('.a, .b { background: var(--banner-err-bg); }')).toEqual(['.a', '.b']);
    expect(
      tinting('@media (min-width: 40rem) { .wide { background: var(--chip-err-bg); } }'),
    ).toEqual(['.wide']);
    expect(tinting('@keyframes blink { from { background: var(--chip-accent-bg); } }')).toEqual([
      '@keyframes blink > from',
    ]);
  });

  test('allow what is listed, by its whole selector', () => {
    expect(tinting('.vs-toast[data-tone="warn"] { background: var(--banner-warn-bg); }')).toEqual(
      [],
    );
    expect(tinting('.vs-toast { background: var(--banner-warn-bg); }')).toEqual(['.vs-toast']);
    expect(tinting('.vs-notice { background: var(--panel); color: var(--warn); }')).toEqual([]);
  });

  test('every selector on the list is still in a stylesheet', () => {
    const seen = new Set(
      sheets().flatMap(sheet => rulesOf(readFileSync(sheet, 'utf8')).map(rule => rule.selector)),
    );
    expect(Object.keys(MAY_TINT).filter(selector => !seen.has(selector))).toEqual([]);
  });

  test('paint only what is listed in the stylesheets', () => {
    expect(sheets().length).toBeGreaterThan(5);
    for (const sheet of sheets()) {
      expect(tinting(readFileSync(sheet, 'utf8')), sheet.pathname).toEqual([]);
    }
  });
});

function sheets(): URL[] {
  return readdirSync(new URL('./', import.meta.url))
    .filter(name => name.endsWith('.css') && name !== 'tokens.css')
    .map(name => new URL(name, import.meta.url));
}
