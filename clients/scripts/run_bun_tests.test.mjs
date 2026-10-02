import assert from 'node:assert/strict';
import {execFile} from 'node:child_process';
import {mkdir, mkdtemp, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import test from 'node:test';
import {fileURLToPath} from 'node:url';
import {promisify} from 'node:util';
import {collectedFileNames, collectionErrors, testFileNames} from './run_bun_tests.mjs';

const RUNNER = resolve(dirname(fileURLToPath(import.meta.url)), 'run_bun_tests.mjs');
const run = promisify(execFile);

const PASSING_TEST =
  'import {test, expect} from "bun:test";\ntest("x", () => expect(1).toBe(1));\n';

// One file per rule in the transcription in `run_bun_tests.mjs`, so a real `bun test` run decides
// whether that transcription is right. The runner exits 0 only when its glob and bun's discovery
// agree exactly, so this fixture pins both directions of every rule at once: the four name
// patterns across the eight extensions, a dot-file that is collected, and the two directory kinds
// and the three non-test names that are not.
const DISCOVERY_FIXTURE = {
  ...Object.fromEntries(
    ['js', 'jsx', 'ts', 'tsx', 'mjs', 'cjs', 'mts', 'cts'].flatMap(extension =>
      ['dotted.test', 'under_test', 'dotted.spec', 'under_spec'].map(stem => [
        `src/${stem}.${extension}`,
        PASSING_TEST,
      ]),
    ),
  ),
  'src/nested/.dotfile.test.ts': PASSING_TEST,
  'src/.hidden/skipped.test.ts': PASSING_TEST,
  'src/node_modules/vendored.test.ts': PASSING_TEST,
  'src/helper.ts': PASSING_TEST,
  'src/test.ts': PASSING_TEST,
  'src/testy.ts': PASSING_TEST,
};

test('the glob matches every file bun discovers under the test roots, and nothing else', async t => {
  const root = await fixture(t, 'vibesys-bun-discovery-', DISCOVERY_FIXTURE);

  assert.deepEqual(testFileNames(root, ['./src']), [
    'src/dotted.spec.cjs',
    'src/dotted.spec.cts',
    'src/dotted.spec.js',
    'src/dotted.spec.jsx',
    'src/dotted.spec.mjs',
    'src/dotted.spec.mts',
    'src/dotted.spec.ts',
    'src/dotted.spec.tsx',
    'src/dotted.test.cjs',
    'src/dotted.test.cts',
    'src/dotted.test.js',
    'src/dotted.test.jsx',
    'src/dotted.test.mjs',
    'src/dotted.test.mts',
    'src/dotted.test.ts',
    'src/dotted.test.tsx',
    'src/nested/.dotfile.test.ts',
    'src/under_spec.cjs',
    'src/under_spec.cts',
    'src/under_spec.js',
    'src/under_spec.jsx',
    'src/under_spec.mjs',
    'src/under_spec.mts',
    'src/under_spec.ts',
    'src/under_spec.tsx',
    'src/under_test.cjs',
    'src/under_test.cts',
    'src/under_test.js',
    'src/under_test.jsx',
    'src/under_test.mjs',
    'src/under_test.mts',
    'src/under_test.ts',
    'src/under_test.tsx',
  ]);
  // The gate compares its glob against bun's report, so a clean exit is the assertion that the
  // list above is bun's discovery and not merely this module's idea of it.
  assert.deepEqual(await runnerResult(root, ['./src']), {code: 0, missing: []});
});

test('an unimportable test file is named instead of counting as one failure', async t => {
  // #1039's signature: `dist` is cleared mid-run, one file's import fails, and bun reports `1
  // fail` while that file's tests never register.
  const root = await fixture(t, 'vibesys-bun-unimportable-', {
    'src/present.test.ts': PASSING_TEST,
    'src/absent-dependency.test.ts': `import "@vibesys/gone";\n${PASSING_TEST}`,
  });

  assert.deepEqual(await runnerResult(root, ['./src']), {
    code: 1,
    missing: ['src/absent-dependency.test.ts'],
  });
});

test('a suite that collects fewer files than the package holds fails a passing bun run', async t => {
  // bun exits 0 here: it discovers the file, loads it, finds no test in it, and reports the rest
  // as a clean run. Nothing in its output distinguishes that from a complete suite.
  const root = await fixture(t, 'vibesys-bun-under-collected-', {
    'src/present.test.ts': PASSING_TEST,
    'src/registers-nothing.test.ts': 'export {};\n',
  });

  assert.deepEqual(await runnerResult(root, ['./src']), {
    code: 1,
    missing: ['src/registers-nothing.test.ts'],
  });
});

test('every test root is globbed', async t => {
  const root = await fixture(t, 'vibesys-bun-roots-', {
    'src/unit.test.ts': PASSING_TEST,
    'dev/harness.test.ts': PASSING_TEST,
  });

  assert.deepEqual(testFileNames(root, ['./src', './dev']), [
    'dev/harness.test.ts',
    'src/unit.test.ts',
  ]);
  // A root left out of the arguments is a whole directory of tests that silently stops running,
  // so the gate must not treat the files it did not ask for as surplus either.
  assert.deepEqual(await runnerResult(root, ['./src']), {code: 0, missing: []});
  assert.deepEqual(await runnerResult(root, ['./src', './dev']), {code: 0, missing: []});
});

test('the runner rejects an argument that is not a test root', async t => {
  const root = await fixture(t, 'vibesys-bun-flag-', {'src/unit.test.ts': PASSING_TEST});

  const {code, stderr} = await runner(root, ['./src', '-t', 'name']);

  assert.equal(code, 1);
  assert.match(stderr, /-t: this runner takes test roots only/);
});

test('the runner names a test root it cannot list', async t => {
  const root = await fixture(t, 'vibesys-bun-absent-root-', {'src/unit.test.ts': PASSING_TEST});

  const {code, stderr} = await runner(root, ['./src', './dev']);

  assert.equal(code, 1);
  assert.match(stderr, /dev: cannot list this test root/);
});

test("a root with no test file keeps bun's own diagnosis", async t => {
  const root = await fixture(t, 'vibesys-bun-no-tests-', {'src/helper.ts': 'export {};\n'});

  const {code, stderr} = await runner(root, ['./src']);

  // bun writes no report in this case, and it already exits non-zero naming the naming rule. The
  // gate must not replace that with a complaint about the missing report.
  assert.equal(code, 1);
  assert.match(stderr, /Tests need/);
  assert.doesNotMatch(stderr, /wrote no report/);
});

test('a file bun collects that the glob did not expect is reported, not ignored', () => {
  // The direction a real run cannot produce while the transcription is right. It is the failure a
  // future bun would cause by widening its discovery rules, and rounding it off would let the gate
  // pass a suite it never checked.
  assert.deepEqual(
    collectionErrors(['src/a.test.ts'], new Set(['src/a.test.ts', 'src/b.test.mts'])),
    [
      'The test runner collected files that scripts/run_bun_tests.mjs did not expect, so its ' +
        "transcription of bun's discovery rules is out of date:\n  - src/b.test.mts",
    ],
  );
});

test('a report attribute keeps the path its entities encode', () => {
  assert.deepEqual(
    collectedFileNames('<testsuite name="s" file="src/a &amp;&lt;b&gt;.test.ts" tests="1" />'),
    new Set(['src/a &<b>.test.ts']),
  );
});

/** A throwaway package directory holding `files`, removed when the test that asked for it ends. */
async function fixture(t, prefix, files) {
  const root = await mkdtemp(join(tmpdir(), prefix));
  t.after(() => rm(root, {recursive: true, force: true}));
  await writeFile(join(root, 'package.json'), JSON.stringify({name: 'fixture', private: true}));
  for (const [file, source] of Object.entries(files)) {
    await mkdir(dirname(join(root, file)), {recursive: true});
    await writeFile(join(root, file), source);
  }
  return root;
}

/** The runner's exit code and the files its completeness gate named, for an assertion on both. */
async function runnerResult(root, roots) {
  const {code, stderr} = await runner(root, roots);
  return {code, missing: [...stderr.matchAll(/^ {2}- (.+)$/gm)].map(match => match[1])};
}

async function runner(root, roots) {
  try {
    const {stderr} = await run(process.execPath, [RUNNER, ...roots], {cwd: root});
    return {code: 0, stderr};
  } catch (error) {
    if (typeof error.code !== 'number') throw error;
    return {code: error.code, stderr: error.stderr};
  }
}
