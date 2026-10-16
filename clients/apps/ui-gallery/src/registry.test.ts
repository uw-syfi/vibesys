import {expect, test} from 'bun:test';
import * as ui from '@vibesys/ui';
import {themeFromQuery} from './gallery.js';
import {REGISTRY} from './registry.js';

test('test_gallery_covers_every_public_component', () => {
  const components = Object.entries(ui)
    .filter(([name, value]) => typeof value === 'function' && /^[A-Z]/.test(name))
    .map(([name]) => name)
    .sort();
  expect(Object.keys(REGISTRY).sort()).toEqual(components);
});

test('the theme query names one of the six themes or none', () => {
  expect(themeFromQuery('?theme=midnight')).toBe('midnight');
  expect(themeFromQuery('?theme=Midnight')).toBeNull();
  expect(themeFromQuery('')).toBeNull();
});
