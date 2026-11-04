import {describe, expect, test} from 'bun:test';
import {applyTheme, type ThemeName} from './index.js';
import {THEMES} from './tokens/index.js';

class FakeElement {
  readonly attributes = new Map<string, string>();
  setAttribute(name: string, value: string): void {
    this.attributes.set(name, value);
  }
}

describe('applyTheme', () => {
  test('sets data-theme to the theme name, replacing the previous one', () => {
    const element = new FakeElement();
    for (const name of Object.keys(THEMES) as ThemeName[]) {
      applyTheme(name, element);
      expect(element.attributes.get('data-theme')).toBe(name);
    }
    expect(element.attributes.size).toBe(1);
  });
});
