import {expect, test} from 'bun:test';
import {transportNode} from './index.js';

test('the . entry loads', () => {
  expect(transportNode).toBe('transport-node');
});
