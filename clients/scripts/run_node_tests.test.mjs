import assert from 'node:assert/strict';
import {execFile} from 'node:child_process';
import {mkdir, mkdtemp, readFile, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import test from 'node:test';
import {fileURLToPath} from 'node:url';
import {promisify} from 'node:util';
import {compiledTestFileNames, missingCompiledTestFileNames} from './run_node_tests.mjs';

const RUNNER = resolve(dirname(fileURLToPath(import.meta.url)), 'run_node_tests.mjs');
const BACKEND_MANIFEST = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../backend-client/package.json',
);
const run = promisify(execFile);

test('the backend-client package gate runs the same tests under Bun and Node', async () => {
  const manifest = JSON.parse(await readFile(BACKEND_MANIFEST, 'utf8'));

  assert.equal(manifest.scripts.test, 'pnpm test:bun && pnpm test:node');
  assert.equal(manifest.scripts['test:bun'], 'node ../scripts/run_bun_tests.mjs ./src');
  assert.equal(manifest.scripts['test:node'], 'node ../scripts/run_node_tests.mjs ./src');
});

test('TypeScript module kinds map to the files Node executes', () => {
  assert.deepEqual(
    compiledTestFileNames(
      ['src/a.test.ts', 'src/b.test.tsx', 'src/c.test.mts', 'src/d.test.cts'],
      '/build',
    ),
    [
      '/build/src/a.test.js',
      '/build/src/b.test.js',
      '/build/src/c.test.mjs',
      '/build/src/d.test.cjs',
    ],
  );
});

test('every discovered test file is compiled and explicitly executed', async t => {
  const root = await fixture(t, {
    'src/first.test.ts': 'const value: number = 1; if (value !== 1) throw new Error("first");\n',
    'src/nested/second.test.ts':
      'const value: string = "second"; if (value.length !== 6) throw new Error(value);\n',
  });

  assert.equal((await runnerResult(root)).code, 0);
});

test('missing compiler outputs are named as incomplete', async t => {
  const root = await fixture(
    t,
    {
      'src/included.test.ts': 'export {};\n',
      'src/omitted.test.ts': 'export {};\n',
    },
    ['src/included.test.ts'],
  );

  const emitted = compiledTestFileNames(
    ['src/included.test.ts', 'src/omitted.test.ts'],
    join(root, 'build'),
  );
  await mkdir(dirname(emitted[0]), {recursive: true});
  await writeFile(emitted[0], 'export {};\n');
  assert.deepEqual(missingCompiledTestFileNames(emitted), [emitted[1]]);

  const result = await runnerResult(root);
  assert.equal(result.code, 1);
  assert.match(result.output, /omitted\.test\.js/);
});

test('a failure in any compiled test fails the lane and names the file', async t => {
  const root = await fixture(t, {
    'src/passes.test.ts': 'export {};\n',
    'src/fails.test.ts': 'throw new Error("observable failure");\n',
  });

  const result = await runnerResult(root);
  assert.equal(result.code, 1);
  assert.match(result.output, /fails\.test\.js/);
});

async function fixture(t, files, include = ['src/**/*.ts']) {
  const root = await mkdtemp(join(tmpdir(), 'vibesys-node-runner-'));
  t.after(() => rm(root, {recursive: true, force: true}));
  await writeFile(join(root, 'package.json'), '{"type":"module"}\n');
  await writeFile(
    join(root, 'tsconfig.node-tests.json'),
    `${JSON.stringify(
      {
        compilerOptions: {
          target: 'ES2022',
          module: 'NodeNext',
          moduleResolution: 'NodeNext',
          strict: true,
          rootDir: '.',
          types: [],
        },
        include,
      },
      null,
      2,
    )}\n`,
  );
  for (const [relative, source] of Object.entries(files)) {
    const path = join(root, relative);
    await mkdir(dirname(path), {recursive: true});
    await writeFile(path, source);
  }
  return root;
}

async function runnerResult(root) {
  const env = {...process.env};
  // The fixture starts its own `node --test` process. Do not let Node mistake
  // that independent runner for a child test of this file's outer runner.
  delete env.NODE_TEST_CONTEXT;
  try {
    const result = await run(process.execPath, [RUNNER, './src'], {cwd: root, env});
    return {code: 0, output: result.stdout + result.stderr};
  } catch (error) {
    return {code: error.code, output: error.stdout + error.stderr};
  }
}
