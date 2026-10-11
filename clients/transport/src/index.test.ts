import {expect, test} from 'bun:test';
import {transport} from './index.js';

test('the . entry loads', () => {
  expect(transport).toBe('transport');
});
