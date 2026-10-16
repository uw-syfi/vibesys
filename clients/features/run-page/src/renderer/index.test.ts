import {expect, test} from 'bun:test';
import {runPageRenderer} from './index.js';

test('the ./renderer entry loads', () => {
  expect(runPageRenderer).toBe('run-page');
});
