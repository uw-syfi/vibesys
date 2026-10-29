import {describe, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {TYPE} from './tokens/index.js';

const css = readFileSync(new URL('./fonts.css', import.meta.url), 'utf8');
const urls = [...css.matchAll(/url\("([^"]+)"\)/g)].map(match => match[1] ?? '');

describe('fonts.css', () => {
  test('bundles both families at the weights the type scale uses, as woff2 files that exist', () => {
    expect(urls).toHaveLength(7);
    for (const url of urls) {
      const bytes = readFileSync(new URL(url, import.meta.url));
      expect(bytes.subarray(0, 4).toString('latin1')).toBe('wOF2');
    }
    const shipped = new Set(
      [...css.matchAll(/font-weight: (\d+);/g)].map(match => Number(match[1])),
    );
    expect([...shipped].sort()).toEqual(
      [...new Set(Object.values(TYPE).map(spec => spec.weight))].sort(),
    );
  });

  test('fetches nothing from the network', () => {
    expect(css).not.toMatch(/https?:|\/\//);
    expect(urls.every(url => url.startsWith('./fonts/'))).toBe(true);
  });
});
