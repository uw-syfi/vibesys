import assert from 'node:assert/strict';
import {test} from 'node:test';
import {assignE2eSpecs, e2eShard, e2eSpecs, e2eTestMatch} from '../e2e/shard.mts';

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
  for (const value of [
    '0/5',
    '6/5',
    '1',
    '1/0',
    'a/b',
    '1/5/7',
    ' 1/5',
    '-1/5',
    '1/99999999999999999999',
  ]) {
    assert.throws(() => e2eShard(VARIABLE, {[VARIABLE]: value}), new RegExp(VARIABLE), value);
  }
});

test('duration plans partition every generated suite and are independent of discovery order', () => {
  for (let size = 1; size <= 80; size += 1) {
    const specs = Array.from({length: size}, (_, i) => `app/e2e/spec-${i}.spec.ts`);
    const seconds = Object.fromEntries(specs.map((path, i) => [path, ((i * 37) % 97) + 1]));
    for (let total = 1; total <= 8; total += 1) {
      const plan = assignE2eSpecs(specs, seconds, total);
      assert.equal(plan.size, size);
      assert.deepEqual(plan, assignE2eSpecs([...specs].reverse(), seconds, total));
      const loads = Array(total).fill(0);
      for (const path of specs) {
        const shard = plan.get(path);
        assert.ok(shard >= 1 && shard <= total);
        loads[shard - 1] += seconds[path];
      }
      assert.ok(Math.max(...loads) - Math.min(...loads) <= Math.max(...Object.values(seconds)));
    }
  }
});

test('the heavy Electron and gallery specs share a global plan with shell specs', () => {
  const seconds = {
    'desktop-main/e2e/gateway.spec.ts': 94,
    'ui-gallery/e2e/gallery.spec.ts': 26,
    'desktop-shell/e2e/relaunch.spec.ts': 50,
    'desktop-shell/e2e/layout.spec.ts': 80,
    'desktop-shell/e2e/run.spec.ts': 70,
  };
  const plan = assignE2eSpecs(Object.keys(seconds), seconds, 5);
  assert.equal(new Set(plan.values()).size, 5);
});

test('new specs without a timing are retained and invalid inputs fail by name', () => {
  assert.equal(assignE2eSpecs(['new.spec.ts'], {}, 2).size, 1);
  assert.throws(() => assignE2eSpecs(['a', 'a'], {}, 2), /duplicate/);
  for (const total of [0, -1, 1.5, Infinity]) {
    assert.throws(() => assignE2eSpecs([], {}, total), /total/);
  }
  for (const duration of [0, -1, NaN, Infinity]) {
    assert.throws(() => assignE2eSpecs(['a'], {a: duration}, 2), /a: invalid/);
  }
});

test('every repository spec belongs to exactly one app slice for every matrix size', () => {
  const specs = e2eSpecs();
  assert.ok(specs.length > 0);
  const apps = [...new Set(specs.map(path => path.split('/')[0]))];
  const expected = specs.map(path => `**/${path}`).sort();
  for (let total = 1; total <= 8; total += 1) {
    const collected = [];
    for (let current = 1; current <= total; current += 1) {
      for (const app of apps) {
        collected.push(...e2eTestMatch(app, {[VARIABLE]: `${current}/${total}`}));
      }
    }
    assert.deepEqual(collected.sort(), expected);
  }
  assert.deepEqual(apps.flatMap(app => e2eTestMatch(app, {})).sort(), expected);
  assert.throws(() => e2eTestMatch('missing-app', {}), /missing-app: no e2e specs/);
});
