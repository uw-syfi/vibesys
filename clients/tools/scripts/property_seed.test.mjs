import assert from 'node:assert/strict';
import test from 'node:test';
import fc from 'fast-check';
import {
  FIXED_SEED,
  PATH_VARIABLE,
  propertyParameters,
  propertySeed,
  SEED_VARIABLE,
} from './property_seed.mjs';
import {RUNNER_ARGUMENTS} from './run_bun_tests.mjs';
import {PROPERTY_SEED_SETUP} from './test_runner_config.mjs';
import vitestConfig from './vitest.config.mjs';

const seedOf = value => propertySeed({[SEED_VARIABLE]: value});

test('unset or empty is the fixed seed, whatever else the environment holds', () => {
  assert.equal(propertySeed({}), FIXED_SEED);
  assert.equal(seedOf(''), FIXED_SEED);
  assert.equal(seedOf('  '), FIXED_SEED);
});

test('a decimal integer is that seed', () => {
  fc.assert(
    fc.property(fc.integer(), n => {
      assert.equal(seedOf(String(n)), n);
    }),
    {seed: FIXED_SEED},
  );
});

test('random draws a 32-bit integer that follows the draw', () => {
  fc.assert(
    fc.property(fc.double({min: 0, max: 1, maxExcluded: true, noNaN: true}), draw => {
      const seed = propertySeed({[SEED_VARIABLE]: 'random'}, () => draw);
      assert.ok(Number.isInteger(seed) && seed >= -(2 ** 31) && seed < 2 ** 31);
    }),
    {seed: FIXED_SEED},
  );
  const draws = [0.1, 0.7].map(draw => propertySeed({[SEED_VARIABLE]: 'random'}, () => draw));
  assert.notEqual(draws[0], draws[1]);
});

test('anything else is rejected, naming the variable', () => {
  for (const bad of ['1.5', 'abc', '0x10', 'Random', '1e3', '99999999999999999999']) {
    assert.throws(() => seedOf(bad), new RegExp(SEED_VARIABLE));
  }
});

test('a path is passed on only when given', () => {
  assert.deepEqual(propertyParameters({}), {seed: FIXED_SEED});
  assert.deepEqual(propertyParameters({[SEED_VARIABLE]: '7', [PATH_VARIABLE]: '3:1'}), {
    seed: 7,
    path: '3:1',
  });
});

test('both shared runners load the seed setup', () => {
  assert.ok(vitestConfig.test.setupFiles.includes(PROPERTY_SEED_SETUP));
  const preload = RUNNER_ARGUMENTS.indexOf('--preload');
  assert.equal(RUNNER_ARGUMENTS[preload + 1], PROPERTY_SEED_SETUP);
});
