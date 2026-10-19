import assert from 'node:assert/strict';
import {test} from 'node:test';
import {e2eShard} from '../e2e/shard.mts';

const VARIABLE = 'VIBESYS_E2E_SHARD';

test('an unset or empty variable runs the whole suite', () => {
  assert.equal(e2eShard(VARIABLE, {}), undefined);
  assert.equal(e2eShard(VARIABLE, {[VARIABLE]: ''}), undefined);
});

test('every slice of a matrix parses to its own position', () => {
  for (let total = 1; total <= 8; total += 1) {
    for (let current = 1; current <= total; current += 1) {
      assert.deepEqual(e2eShard(VARIABLE, {[VARIABLE]: `${current}/${total}`}), {current, total});
    }
  }
});

test('a malformed or out-of-range value fails naming the variable', () => {
  for (const value of ['0/5', '6/5', '1', '1/0', 'a/b', '1/5/7', ' 1/5', '-1/5']) {
    assert.throws(() => e2eShard(VARIABLE, {[VARIABLE]: value}), new RegExp(VARIABLE), value);
  }
});
