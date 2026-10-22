import assert from 'node:assert/strict';
import test from 'node:test';
import {testRunnerCacheErrors, testRunnerErrors} from './check_test_runners.mjs';
import {NODE_TEST_ARGUMENTS, PER_TEST_BOUND_MS} from './test_runner_config.mjs';

for (const runtime of ['bun test', 'node --test', 'node --test --test-timeout=0']) {
  for (const prefix of ['', 'tsc -b && ', 'FLAG=value ']) {
    test(`reject an unguarded or overridden runner: ${prefix}${runtime}`, () => {
      const errors = testRunnerErrors('libs/new-package/package.json', {
        scripts: {test: `${prefix}${runtime} tests/*.test.mjs`},
      });
      assert.equal(errors.length, 1);
      assert.match(errors[0], /libs\/new-package\/package.json.*scripts.test/);
    });
  }
}
for (const runner of ['bun_tests', 'node_tests', 'node_suite']) {
  test(`${runner} keeps shared policy and rejects package overrides`, () => {
    const script = `node ../../tools/scripts/run_${runner}.mjs ./src`;
    assert.deepEqual(testRunnerErrors('package.json', {scripts: {test: script}}), []);
    for (const flag of ['--timeout 0', '--test-timeout=0', '--preload bypass.mjs']) {
      assert.equal(
        testRunnerErrors('package.json', {scripts: {test: `${script} ${flag}`}}).length,
        1,
      );
    }
  });
}
test('both runtimes use the same generous hang guard', () => {
  assert.equal(PER_TEST_BOUND_MS, 60_000);
  assert.ok(NODE_TEST_ARGUMENTS.includes(`--test-timeout=${PER_TEST_BOUND_MS}`));
  assert.ok(NODE_TEST_ARGUMENTS.includes('--test-concurrency=1'));
});
test('delegated and browser test scripts keep their own runtime', () => {
  assert.deepEqual(
    testRunnerErrors('package.json', {
      scripts: {test: 'pnpm test:bun && pnpm test:node', 'test:e2e': 'playwright test'},
    }),
    [],
  );
});

test('every runner and its shared policy invalidate cached package tests', () => {
  const files = [
    'tools/scripts/run_bun_tests.mjs',
    'tools/scripts/run_node_tests.mjs',
    'tools/scripts/run_node_suite.mjs',
    'tools/scripts/test_runner_config.mjs',
  ];
  assert.deepEqual(testRunnerCacheErrors({globalDependencies: files}), []);
  for (const missing of files) {
    const errors = testRunnerCacheErrors({
      globalDependencies: files.filter(file => file !== missing),
    });
    assert.equal(errors.length, 1);
    assert.ok(errors[0].includes(missing));
    assert.match(errors[0], /turbo.json/);
  }
  assert.equal(testRunnerCacheErrors({}).length, files.length);
});
