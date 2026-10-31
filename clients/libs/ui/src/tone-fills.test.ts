/**
 * The app draws no banner: no full-width strip tinted by a tone. The tone fills (`--banner-*-bg`)
 * paint a toast and an attention tile and nothing else, so a panel that tints itself with them, as
 * the deleted `Banner` did, cannot be added without failing here.
 */

import {describe, expect, test} from 'bun:test';
import {readdirSync, readFileSync} from 'node:fs';

/** The selectors that may paint with a tone fill. */
const MAY_TINT = [/^\.vs-toast(\[|$)/, /^\.vs-overview-tile(\[|$)/];

/** The selectors of the rules in `css` that paint with a tone fill and may not. */
function tinting(css: string): string[] {
  const rules = [...css.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/([^{}]+)\{([^{}]*)\}/g)];
  return rules
    .filter(rule => /var\(--banner-[a-z]+-bg\)/.test(rule[2] ?? ''))
    .flatMap(rule => (rule[1] ?? '').split(',').map(selector => selector.trim()))
    .filter(selector => !MAY_TINT.some(allowed => allowed.test(selector)));
}

describe('the tone fills', () => {
  test('refuse a panel that tints itself with them', () => {
    expect(tinting('.vs-notice { background: var(--banner-warn-bg); }')).toEqual(['.vs-notice']);
    expect(
      tinting(
        '.vs-notice[data-tone="failed"], .vs-toast[data-tone="err"] { background: var(--banner-err-bg); }',
      ),
    ).toEqual(['.vs-notice[data-tone="failed"]']);
    expect(tinting('.vs-toast[data-tone="warn"] { background: var(--banner-warn-bg); }')).toEqual(
      [],
    );
  });

  test('paint a toast and an attention tile and nothing else in the stylesheets', () => {
    const sheets = readdirSync(new URL('./', import.meta.url)).filter(
      name => name.endsWith('.css') && name !== 'tokens.css',
    );
    expect(sheets.length).toBeGreaterThan(5);
    for (const sheet of sheets) {
      const css = readFileSync(new URL(sheet, import.meta.url), 'utf8');
      expect(tinting(css), sheet).toEqual([]);
    }
  });
});
