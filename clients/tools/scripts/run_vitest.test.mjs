import assert from 'node:assert/strict';
import test from 'node:test';
import {parseRoots} from './run_vitest.mjs';

test('roots become path filters without a leading ./ or trailing slash', () => {
  assert.deepEqual(parseRoots(['./src', 'tests/', './a/b//']), ['src', 'tests', 'a/b']);
});

test('no roots is a usage error', () => {
  assert.throws(() => parseRoots([]), /usage/);
});

test('a flag is rejected by name, so the shared hang guard cannot be switched off', () => {
  assert.throws(() => parseRoots(['./src', '--testTimeout=0']), /--testTimeout=0.*roots only/);
});
