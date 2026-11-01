import assert from 'node:assert/strict';
import {readdirSync, readFileSync} from 'node:fs';
import {join} from 'node:path';
import {test} from 'node:test';
import {fileURLToPath} from 'node:url';
import {CAPTURE_VARIABLES, DEFAULT_WORKERS, e2eWorkers, WORKERS_VARIABLE} from '../e2e/workers.mts';

const APPS = fileURLToPath(new URL('../../apps/', import.meta.url));

test('without a count a run uses the default, capped by the machine', () => {
  for (let cores = 1; cores <= 64; cores += 1) {
    assert.equal(e2eWorkers({}, cores), Math.min(DEFAULT_WORKERS, cores));
    assert.equal(e2eWorkers({[WORKERS_VARIABLE]: ''}, cores), Math.min(DEFAULT_WORKERS, cores));
  }
});

test('a named count is used whatever the machine has', () => {
  for (let count = 1; count <= 64; count += 1) {
    assert.equal(e2eWorkers({[WORKERS_VARIABLE]: String(count)}, 4), count);
  }
});

test('a count that is not a positive whole number fails naming the variable', () => {
  for (const value of ['0', '-1', '1.5', 'four', '4 ', ' 4', '04', '1e1', '99999999999999999999']) {
    assert.throws(
      () => e2eWorkers({[WORKERS_VARIABLE]: value}),
      new RegExp(WORKERS_VARIABLE),
      value,
    );
  }
});

test('a run that captures screens uses one worker, and refuses a count above one', () => {
  for (const name of CAPTURE_VARIABLES) {
    assert.equal(e2eWorkers({[name]: '/somewhere'}, 12), 1, name);
    assert.equal(e2eWorkers({[name]: '/somewhere', [WORKERS_VARIABLE]: '1'}, 12), 1, name);
    assert.throws(
      () => e2eWorkers({[name]: '/somewhere', [WORKERS_VARIABLE]: '2'}, 12),
      new RegExp(`${name}.*${WORKERS_VARIABLE}`),
      name,
    );
    assert.equal(e2eWorkers({[name]: ''}, 12), DEFAULT_WORKERS, `${name} empty is not set`);
  }
});

/** The `.ts` files under `directory`, other than those of installed packages and hidden folders. */
function sourceFiles(directory) {
  return readdirSync(directory, {withFileTypes: true}).flatMap(entry => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) {
      return entry.name === 'node_modules' || entry.name.startsWith('.') ? [] : sourceFiles(path);
    }
    return entry.name.endsWith('.ts') ? [path] : [];
  });
}

/** Every environment variable a spec reads by name, other than the suite's own `VIBESYS_` and `ALLOW_` ones. */
function specVariables() {
  const found = new Set();
  for (const app of readdirSync(APPS)) {
    for (const file of sourceFiles(join(APPS, app, 'e2e'))) {
      for (const match of readFileSync(file, 'utf8').matchAll(/process\.env\['([A-Z_]+)'\]/g)) {
        found.add(match[1]);
      }
    }
  }
  return [...found].filter(name => !/^(VIBESYS_|ALLOW_)/.test(name)).sort();
}

test('every variable a spec reads to write screens is a capture variable', () => {
  assert.deepEqual(specVariables(), [...CAPTURE_VARIABLES].sort());
});
