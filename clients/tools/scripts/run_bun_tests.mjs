#!/usr/bin/env node

/**
 * The clients test runner: `bun test`, plus a gate on what the run actually collected.
 *
 * `bun test` reports a test file it cannot import as a single error and keeps running the rest, so
 * a build artifact that disappears mid-run costs that whole file's tests and still reads as one
 * ordinary failure. Tests resolve workspace packages to their source through the `@vibesys/source`
 * export condition, so no `dist` is on that path any more, but a module that fails to import for
 * any other reason is still invisible. Run 7 of #1039 reported `Ran 1023 tests across 38 files` with
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
 * `--max-concurrency 1` and the source condition live here, and the hang guard lives in the shared
 * runner configuration, rather than in each
 * package's `test` script, so the packages cannot drift apart. The runner takes test roots only:
 * a bun flag that changed which files run (`-t`, `--bail`, `--only`) would make the gate report
 * the rest of the suite as missing, so ad-hoc runs call `bun test` directly.
 */

import {execFileSync, spawn} from 'node:child_process';
import {mkdtempSync, readdirSync, readFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {PER_TEST_BOUND_MS} from './test_runner_config.mjs';

const RUNNER = 'bun';
/**
 * The Bun version is pinned in `packaging/fetch_bun.py`, which CI uses to install it.
 * A Bun older than the pin resolves imports differently (1.3.9 does not map
 * `./welcome.cjs` to its `welcome.cts` source), so a test run on it fails on a module it cannot
 * find, which reads as a missing build and not as the wrong runtime.
 */
const BUN_PIN_FILE = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../../../packaging/fetch_bun.py',
);
const RUNNER_ARGUMENTS = [
  'test',
  '--conditions=@vibesys/source',
  '--max-concurrency',
  '1',
  '--timeout',
  String(PER_TEST_BOUND_MS),
  '--reporter=junit',
];
const REPORT_FILE = 'collected.xml';

/**
 * Bun's test-file discovery rules, transcribed, because the gate has to glob the same set bun
 * does. Verified against bun 1.4.2, the version `packaging/fetch_bun.py` pins: a name is a
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
        '\nRun the file alone with `bun test --conditions=@vibesys/source <file>` to see why it ' +
        'did not load; if the file has no tests at all, give it one or delete it.',
    );
  }
  if (surplus.length > 0) {
    errors.push(
      'The test runner collected files that tools/scripts/run_bun_tests.mjs did not expect, so its ' +
        "transcription of bun's discovery rules is out of date:\n" +
        surplus.map(name => `  - ${name}`).join('\n'),
    );
  }
  return errors;
}

/**
 * The Bun version `packaging/fetch_bun.py` pins, read from its `BUN_VERSION = "x.y.z"` line.
 *
 * @param {string} source the text of that file
 * @returns {string}
 */
export function pinnedBunVersion(source) {
  const match = source.match(/^BUN_VERSION = "(\d+\.\d+\.\d+)"$/m);
  if (match === null) throw new Error(`${BUN_PIN_FILE}: no BUN_VERSION = "x.y.z" line`);
  return match[1];
}

/**
 * The rejection for a Bun other than the pinned one, or `undefined` when they agree.
 *
 * @param {string} actual the version `bun --version` printed
 * @param {string} pinned the version `pinnedBunVersion` read
 * @returns {string | undefined}
 */
export function bunVersionError(actual, pinned) {
  if (actual === pinned) return undefined;
  return (
    `bun ${actual} is on PATH but the clients tests need bun ${pinned} (the pin in ` +
    'packaging/fetch_bun.py, the version CI installs). Another version resolves imports ' +
    'differently, so a test can fail on a module it cannot find. Install the pinned version, ' +
    'for example `python3 packaging/fetch_bun.py --target <linux-x86_64|linux-aarch64|macos-arm64> ' +
    '--output <dir>/bin/bun`, and put that directory first on PATH.'
  );
}

/** The version of the Bun on PATH, or a rejection saying there is none. */
function installedBunVersion() {
  try {
    return execFileSync(RUNNER, ['--version'], {encoding: 'utf8'}).trim();
  } catch (error) {
    throw new Error(`cannot run \`${RUNNER} --version\`: ${error.message}`);
  }
}

/** The test roots relative to the package, or a rejection naming why an argument is not one. */
function parseRoots(argv) {
  if (argv.length === 0) {
    throw new Error('usage: node ../../tools/scripts/run_bun_tests.mjs <test root>...');
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
  const versionError = bunVersionError(
    installedBunVersion(),
    pinnedBunVersion(readFileSync(BUN_PIN_FILE, 'utf8')),
  );
  if (versionError !== undefined) {
    console.error(versionError);
    return 1;
  }
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
