import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {test} from 'node:test';
import {e2eSpecs} from '../e2e/shard.mts';

const SCRIPT = new URL('./with_e2e_specs.mjs', import.meta.url).pathname;

function run(app, env) {
  const result = spawnSync(process.execPath, [SCRIPT, app, 'echo built-and-ran'], {
    encoding: 'utf8',
    env: {...process.env, VIBESYS_E2E_SHARD: '', VIBESYS_E2E_SPECS: '', ...env},
  });
  return {status: result.status, output: result.stdout + result.stderr};
}

test('an app with a spec in the job runs its command', () => {
  const selection = e2eSpecs()
    .filter(spec => spec.startsWith('desktop-shell/'))
    .slice(0, 2);
  const result = run('desktop-shell', {VIBESYS_E2E_SPECS: JSON.stringify(selection)});
  assert.equal(result.status, 0);
  assert.match(result.output, /built-and-ran/);
});

test('an app with no spec in the job builds and runs nothing', () => {
  const selection = e2eSpecs()
    .filter(spec => spec.startsWith('desktop-shell/'))
    .slice(0, 2);
  for (const app of ['desktop-main', 'ui-gallery']) {
    const result = run(app, {VIBESYS_E2E_SPECS: JSON.stringify(selection)});
    assert.equal(result.status, 0, app);
    assert.doesNotMatch(result.output, /built-and-ran/, app);
    assert.match(result.output, new RegExp(`no ${app} spec`), app);
  }
});

test('every app runs its command when the whole suite is planned', () => {
  for (const app of ['desktop-main', 'desktop-shell', 'ui-gallery']) {
    assert.match(run(app, {}).output, /built-and-ran/, app);
  }
});

test('a failing command fails the job', () => {
  const result = spawnSync(process.execPath, [SCRIPT, 'desktop-shell', 'exit 3'], {
    encoding: 'utf8',
    env: {...process.env, VIBESYS_E2E_SHARD: '', VIBESYS_E2E_SPECS: ''},
  });
  assert.equal(result.status, 3);
});

test('a missing command is a usage error', () => {
  const result = spawnSync(process.execPath, [SCRIPT, 'desktop-shell'], {encoding: 'utf8'});
  assert.equal(result.status, 2);
});
