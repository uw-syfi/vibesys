/**
 * What is live is shown by its tone and its words, never by a motion that loops: a bar, an icon or
 * a dot that pulses for as long as the screen is open goes on whether or not the run is live. Only an
 * indeterminate meter, which is a wait for a reply and ends with it, may repeat. Every other
 * animation runs once, on the motion tokens, and `prefers-reduced-motion` sets those to zero.
 */

import {describe, expect, test} from 'bun:test';
import {readdirSync, readFileSync} from 'node:fs';

const dir = new URL('.', import.meta.url);
const stylesheets = readdirSync(dir)
  .filter(name => name.endsWith('.css') && name !== 'tokens.css')
  .map(name => ({name, text: readFileSync(new URL(name, dir), 'utf8')}));

/** The rule text of each `animation` declaration, with the selector it sits in. */
function animations(text: string): {selector: string; value: string}[] {
  const found: {selector: string; value: string}[] = [];
  for (const rule of text.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selector = (rule[1] ?? '').trim();
    for (const line of (rule[2] ?? '').split(';')) {
      const match = /^\s*animation(?:-[a-z-]+)?\s*:\s*(.*)$/s.exec(line);
      if (match !== null) found.push({selector, value: (match[1] ?? '').trim()});
    }
  }
  return found;
}

const MAY_REPEAT = ['.vs-meter-indeterminate'];

describe('motion of what is live', () => {
  test('nothing loops but the indeterminate meter', () => {
    const looping = stylesheets.flatMap(({name, text}) =>
      animations(text)
        .filter(each => /\binfinite\b/.test(each.value) && !MAY_REPEAT.includes(each.selector))
        .map(each => `${name}: ${each.selector}`),
    );
    expect(looping).toEqual([]);
  });

  test('every animation is on the motion tokens, so reduced motion stops it', () => {
    const off = stylesheets.flatMap(({name, text}) =>
      animations(text)
        .filter(each => each.value !== 'none' && !/var\(--duration-/.test(each.value))
        .map(each => `${name}: ${each.selector}`),
    );
    expect(off).toEqual([]);
  });
});
