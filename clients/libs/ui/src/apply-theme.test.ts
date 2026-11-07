import {describe, expect, test} from 'bun:test';
import {applyTheme, type ThemeName} from './index.js';
import {THEMES} from './tokens/index.js';

class FakeElement {
  readonly attributes = new Map<string, string>();
  getAttribute(name: string): string | null {
    return this.attributes.get(name) ?? null;
  }
  setAttribute(name: string, value: string): void {
    this.attributes.set(name, value);
  }
}

const NAMES = Object.keys(THEMES) as ThemeName[];

describe('applyTheme', () => {
  test('sets data-theme to the theme name, replacing the previous one', () => {
    const element = new FakeElement();
    for (const name of NAMES) {
      applyTheme(name, element);
      expect(element.attributes.get('data-theme')).toBe(name);
    }
  });

  test('shows the first theme as it is and every later one with motion', () => {
    const element = new FakeElement();
    applyTheme('paper', element);
    expect(element.attributes.has('data-theme-motion')).toBe(false);
    applyTheme('ink', element);
    expect(element.attributes.has('data-theme-motion')).toBe(true);
    applyTheme('ink', element);
    expect(element.attributes.size).toBe(2);
  });
});
