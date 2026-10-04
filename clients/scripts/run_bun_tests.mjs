#!/usr/bin/env node

/**
 * The clients test runner: `bun test`, plus a gate on what the run actually collected.
 *
 * `bun test` reports a test file it cannot import as a single error and keeps running the rest, so
 * a build artifact that disappears mid-run costs that whole file's tests and still reads as one
 * ordinary failure. Every package's `build` clears its own `dist` before `tsc` writes it, and that
 * `dist` is what the other packages resolve their imports through, so any concurrent build in the
 * same checkout can take it away. Run 7 of #1039 reported `Ran 1023 tests across 38 files` with
 * `1 fail` against a normal 1141 tests: the 118 tests that never registered were invisible in the
 * output, and so was the file they belonged to.
 *
 * Guarantees. The suite's exit status is bun's, except that an incomplete run exits non-zero
 * whatever bun reported, naming every test file that contributed no tests. "Incomplete" is decided
 * by identity and not by a count: the test files on disk under the given roots must be exactly the
 * files the run reported, so a test file that is added, renamed, or moved needs no edit here, and
 * a disagreement in either direction is reported rather than rounded off. Bun's own JUnit report
 * is the record of what it collected, so the gate reads the runner's answer instead of scraping
 * its console summary.
 *
 * `--max-concurrency 1` lives here rather than in each package's `test` script, so the four
 * packages cannot drift apart on it. The runner deliberately takes test roots and nothing else:
 * a bun flag that changed which files run (`-t`, `--bail`, `--only`) would make the gate report
 * the rest of the suite as missing, so ad-hoc runs call `bun test` directly.
 */

import {spawn} from 'node:child_process';
import {mkdtempSync, readdirSync, readFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const RUNNER = 'bun';
const RUNNER_ARGUMENTS = ['test', '--max-concurrency', '1', '--reporter=junit'];
const REPORT_FILE = 'collected.xml';

/**
 * Bun's test-file discovery rules, transcribed, because the gate has to glob the same set bun
 * does. Verified against bun 1.4.2, the version `packaging/tui_packaging.py` pins: a name is a
 * test file when `test` or `spec` is its last dot- or underscore-separated component before the
 * extension, for each of `js`, `jsx`, `ts`, `tsx`, `mjs`, `cjs`, `mts`, and `cts`; a directory
 * whose name starts with a dot or is `node_modules` is not searched, while a *file* whose name
 * starts with a dot is collected. Transcribing rather than deriving is safe here because the gate
 * compares both directions: a rule that drifts shows up as a file bun collected and this glob did
 * not, which fails the gate instead of narrowing it.
 */
const TEST_FILE_NAME = /[._](?:test|spec)\.[cm]?[jt]sx?$/;
const UNSEARCHED_DIRECTORY = 'node_modules';
const METADATA_PREFIX = '.';

/** The five entities an XML attribute value can carry, innermost last. */
const XML_ENTITIES = [
  ['&lt;', '<'],
  ['&gt;', '>'],
  ['&quot;', '"'],
  ['&apos;', "'"],
  ['&amp;', '&'],
];

/**
 * The test files under `roots`, as paths relative to `directory` in path order.
 *
 * @param {string} directory the package directory the roots are relative to
 * @param {string[]} roots the test roots, as the `test` script passes them (`./src`)
 * @returns {string[]}
 */
export function testFileNames(directory, roots) {
  const names = new Set();
  for (const root of roots) {
    for (const name of searchDirectory(directory, normalizeRoot(root))) names.add(name);
  }
  return [...names].sort();
}

/**
 * The test files a run reported, read from bun's JUnit report. Every element the reporter emits
 * for a file carries the same `file` attribute, so the set of attribute values is the set of files
 * that reported at least one test.
 *
 * @param {string} report the JUnit XML bun wrote
 * @returns {Set<string>}
 */
export function collectedFileNames(report) {
  const names = [...report.matchAll(/\sfile="([^"]*)"/g)].map(match => decodeAttribute(match[1]));
  return new Set(names);
}

/**
 * Where the files on disk and the files the run reported disagree.
 *
 * @param {string[]} expected the test files under the roots, from `testFileNames`
 * @param {Set<string>} collected the files the run reported, from `collectedFileNames`
 * @returns {string[]} one message per disagreement, naming the offending files
 */
export function collectionErrors(expected, collected) {
  const errors = [];
  const missing = expected.filter(name => !collected.has(name));
  const surplus = [...collected].filter(name => !expected.includes(name)).sort();
  if (missing.length > 0) {
    errors.push(
      `Incomplete test run: ${missing.length} of ${expected.length} test files reported no ` +
        'tests. `bun test` reports a test file it cannot import as a single error and runs the ' +
        'rest, so the tests in these files did not run:\n' +
        missing.map(name => `  - ${name}`).join('\n') +
        '\nA cleared or half-written dependency `dist` is the usual cause: every package `build` ' +
        'removes its own `dist` before `tsc` rewrites it, so a concurrent build in this checkout ' +
        'can take it away mid-run. Rebuild and re-run; if the file has no tests at all, give it ' +
        'one or delete it.',
    );
  }
  if (surplus.length > 0) {
    errors.push(
      'The test runner collected files that scripts/run_bun_tests.mjs did not expect, so its ' +
        "transcription of bun's discovery rules is out of date:\n" +
        surplus.map(name => `  - ${name}`).join('\n'),
    );
  }
  return errors;
}

/** The test roots relative to the package, or a rejection naming why an argument is not one. */
function parseRoots(argv) {
  if (argv.length === 0) {
    throw new Error('usage: node ../scripts/run_bun_tests.mjs <test root>...');
  }
  for (const argument of argv) {
    if (argument.startsWith('-')) {
      throw new Error(
        `${argument}: this runner takes test roots only, because the completeness gate globs ` +
          'them; run `bun test` directly for ad-hoc flags',
      );
    }
  }
  return argv;
}

/** `./src` and `src/` both name the directory bun's report calls `src`. */
function normalizeRoot(root) {
  return root.replace(/^\.\//, '').replace(/\/+$/, '');
}

function* searchDirectory(directory, relativePath) {
  for (const entry of readEntries(directory, relativePath)) {
    const entryPath = `${relativePath}/${entry.name}`;
    if (entry.isDirectory()) {
      if (entry.name.startsWith(METADATA_PREFIX) || entry.name === UNSEARCHED_DIRECTORY) continue;
      yield* searchDirectory(directory, entryPath);
    } else if (entry.isFile() && TEST_FILE_NAME.test(entry.name)) {
      yield entryPath;
    }
  }
}

function readEntries(directory, relativePath) {
  try {
    return readdirSync(join(directory, relativePath), {withFileTypes: true});
  } catch (error) {
    throw new Error(`${relativePath}: cannot list this test root: ${error.message}`);
  }
}

function decodeAttribute(value) {
  let decoded = value;
  for (const [entity, character] of XML_ENTITIES) decoded = decoded.replaceAll(entity, character);
  return decoded;
}

/** The report bun wrote, or `undefined` when it wrote none (it writes none when it found none). */
function readReport(reportFile) {
  try {
    return readFileSync(reportFile, 'utf8');
  } catch (error) {
    if (error.code === 'ENOENT') return undefined;
    throw new Error(`cannot read ${reportFile}: ${error.message}`);
  }
}

function runSuite(roots, reportFile) {
  return new Promise((settle, reject) => {
    const child = spawn(
      RUNNER,
      [...RUNNER_ARGUMENTS, `--reporter-outfile=${reportFile}`, ...roots],
      {
        stdio: 'inherit',
      },
    );
    child.on('error', reject);
    child.on('close', (status, signal) => settle({status, signal}));
  });
}

async function main(argv) {
  const roots = parseRoots(argv);
  const expected = testFileNames(process.cwd(), roots);
  // The report is this process's to create and to remove, on every path, and a per-run directory
  // keeps two packages' suites from naming the same file under `pnpm -r`.
  const reportDirectory = mkdtempSync(join(tmpdir(), 'vibesys-clients-test-'));
  const reportFile = join(reportDirectory, REPORT_FILE);
  try {
    const {status, signal} = await runSuite(roots, reportFile);
    if (signal !== null) {
      console.error(`${RUNNER} test was killed by ${signal}`);
      return 1;
    }
    const report = readReport(reportFile);
    if (report === undefined) {
      // bun writes no report when it found no test file to run, and it already exits non-zero and
      // says so. A second, vaguer error on top of its own would only obscure it. A clean exit with
      // no report is a different thing: nothing checked the run, so the gate says so.
      if (status !== 0) return status;
      throw new Error(`${RUNNER} test exited 0 but wrote no report to ${reportFile}`);
    }
    const errors = collectionErrors(expected, collectedFileNames(report));
    for (const error of errors) console.error(error);
    return errors.length > 0 ? 1 : status;
  } finally {
    rmSync(reportDirectory, {recursive: true, force: true});
  }
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  // A bad argument and an unlistable root are ordinary usage errors, so they get the message and
  // not a stack trace, and the exit code is this module's rather than the runtime's default for an
  // unhandled rejection.
  try {
    process.exitCode = await main(process.argv.slice(2));
  } catch (error) {
    console.error(error.message);
    process.exitCode = 1;
  }
}
