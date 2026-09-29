import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {applyTheme, initialTheme} from './theme.js';

test('?theme= wins, then the saved choice, then System; unknown values are ignored', () => {
  assert.equal(initialTheme('dark', 'light'), 'dark');
  assert.equal(initialTheme(null, 'light'), 'light');
  assert.equal(initialTheme('sepia', 'nope'), 'system');
  assert.equal(initialTheme(null, null), 'system');
});

test('Light and Dark set data-theme; System removes it so the OS decides', () => {
  const attributes = new Map<string, string>();
  const root = {
    setAttribute: (name: string, value: string) => void attributes.set(name, value),
    removeAttribute: (name: string) => void attributes.delete(name),
  };
  applyTheme(root, 'dark');
  assert.equal(attributes.get('data-theme'), 'dark');
  applyTheme(root, 'system');
  assert.equal(attributes.has('data-theme'), false);
});
