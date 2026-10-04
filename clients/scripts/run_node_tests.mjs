#!/usr/bin/env node

/**
 * Compile and run one package's TypeScript tests under the shipping Node runtime.
 *
 * The caller supplies test roots, as it does for `run_bun_tests.mjs`. The files
 * discovered under those roots are the exact files passed to `node --test` after
 * compilation. An omitted output is an incomplete run and fails before Node
 * starts, so adding or moving a test file requires no runner allowlist update.
 *
 * Packages opt in with a `tsconfig.node-tests.json` that emits production and
 * test sources into the temporary directory supplied here. Keeping the config
 * in the package lets TypeScript validate that package's own public/runtime
 * contract, while this runner owns discovery, resource cleanup, and process
 * failure propagation for every package that adopts the lane.
 */

import {spawn} from 'node:child_process';
import {existsSync, mkdtempSync, rmSync} from 'node:fs';
import {join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {testFileNames} from './run_bun_tests.mjs';

const CONFIG = 'tsconfig.node-tests.json';
const BUILD_PREFIX = '.vibesys-node-tests-';
const TYPESCRIPT_COMPILER = fileURLToPath(
  new URL('../node_modules/typescript/bin/tsc', import.meta.url),
);

/** Map TypeScript test source paths to the paths emitted by NodeNext TypeScript. */
export function compiledTestFileNames(sourceNames, outputDirectory) {
  return sourceNames.map(name => {
    let emitted;
    if (name.endsWith('.mts')) emitted = `${name.slice(0, -4)}.mjs`;
    else if (name.endsWith('.cts')) emitted = `${name.slice(0, -4)}.cjs`;
    else if (name.endsWith('.ts') || name.endsWith('.tsx')) {
      emitted = `${name.slice(0, name.lastIndexOf('.'))}.js`;
    } else {
      throw new Error(
        `${name}: the Node lane compiles TypeScript tests only; use a .ts, .tsx, .mts, or .cts file`,
      );
    }
    return join(outputDirectory, emitted);
  });
}

/** Emitted paths absent after a successful compiler exit. */
export function missingCompiledTestFileNames(compiledNames) {
  return compiledNames.filter(name => !existsSync(name));
}

/** Test roots relative to the package, or a rejection naming the unsupported argument. */
function parseRoots(argv) {
  if (argv.length === 0)
    throw new Error('usage: node ../scripts/run_node_tests.mjs <test root>...');
  for (const argument of argv) {
    if (argument.startsWith('-')) {
      throw new Error(
        `${argument}: this runner takes test roots only, because complete collection depends on ` +
          'discovering every file under them',
      );
    }
  }
  return argv;
}

function runProcess(command, args) {
  return new Promise((settle, reject) => {
    const child = spawn(command, args, {stdio: 'inherit'});
    child.on('error', reject);
    child.on('close', (status, signal) => settle({status, signal}));
  });
}

function exitCode(result, command) {
  if (result.signal !== null) {
    console.error(`${command} was killed by ${result.signal}`);
    return 1;
  }
  return result.status ?? 1;
}

async function main(argv) {
  const roots = parseRoots(argv);
  const sourceNames = testFileNames(process.cwd(), roots);
  if (sourceNames.length === 0) throw new Error('the Node test roots contain no TypeScript tests');

  const buildDirectory = mkdtempSync(join(process.cwd(), BUILD_PREFIX));
  try {
    const compile = await runProcess(process.execPath, [
      TYPESCRIPT_COMPILER,
      '--project',
      CONFIG,
      '--outDir',
      buildDirectory,
    ]);
    const compileCode = exitCode(compile, 'TypeScript');
    if (compileCode !== 0) return compileCode;

    const compiledNames = compiledTestFileNames(sourceNames, buildDirectory);
    const missing = missingCompiledTestFileNames(compiledNames);
    if (missing.length > 0) {
      throw new Error(
        `Incomplete Node test compilation: ${missing.length} of ${sourceNames.length} test files ` +
          `were not emitted:\n${missing.map(name => `  - ${name}`).join('\n')}`,
      );
    }

    const result = await runProcess(process.execPath, [
      '--test',
      '--test-concurrency=1',
      ...compiledNames,
    ]);
    return exitCode(result, 'node --test');
  } finally {
    rmSync(buildDirectory, {recursive: true, force: true});
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    process.exitCode = await main(process.argv.slice(2));
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
