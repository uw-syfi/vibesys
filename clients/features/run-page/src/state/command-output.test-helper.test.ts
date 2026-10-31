import {expect, test} from 'bun:test';
import * as fc from 'fast-check';
import {commandOutput} from './command-output.test-helper.js';

test('the command output generator reaches every input the line properties are named for', () => {
  const sample = fc.sample(commandOutput, {numRuns: 600, seed: 1});
  expect(sample.some(text => text.includes('\n'))).toBe(true);
  expect(sample.some(text => text.includes('\r\n'))).toBe(true);
  expect(sample.some(text => text.endsWith('\n'))).toBe(true);
  expect(sample.some(text => /^\n+$/.test(text))).toBe(true);
  expect(sample.some(text => /\P{ASCII}/u.test(text))).toBe(true);
  expect(sample.some(text => text === '')).toBe(true);
  expect(sample.some(text => new TextEncoder().encode(text).length > 65_536)).toBe(true);
});
