import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {SIDE_ACTION_MIN_WIDTH} from './components/side-action-layout.js';

test('the width at which the action panel moves beside the form is the CSS media query the hook reads', () => {
  const css = readFileSync(new URL('./base.css', import.meta.url), 'utf8');
  const block = css.slice(css.indexOf('/* A long form keeps its checks'));
  expect(block.split('\n')[1]).toBe(`@media (min-width: ${SIDE_ACTION_MIN_WIDTH}px) {`);
});
