import {describe, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';

const css = readFileSync(new URL('./fonts.css', import.meta.url), 'utf8');
const urls = [...css.matchAll(/url\("([^"]+)"\)/g)].map(match => match[1] ?? '');

describe('fonts.css', () => {
  test('bundles both families at weights 400, 500 and 600, as woff2 files that exist', () => {
    expect(urls).toHaveLength(6);
    for (const url of urls) {
      const bytes = readFileSync(new URL(url, import.meta.url));
      expect(bytes.subarray(0, 4).toString('latin1')).toBe('wOF2');
    }
    const weights = [...css.matchAll(/font-weight: (\d+);/g)].map(match => match[1]);
    expect(weights.sort()).toEqual(['400', '400', '500', '500', '600', '600']);
  });

  test('fetches nothing from the network', () => {
    expect(css).not.toMatch(/https?:|\/\//);
    expect(urls.every(url => url.startsWith('./fonts/'))).toBe(true);
  });
});
