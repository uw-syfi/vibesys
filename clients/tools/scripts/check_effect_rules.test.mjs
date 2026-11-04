import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdir, mkdtemp, readFile, rm, writeFile} from 'node:fs/promises';
import {dirname, join, resolve} from 'node:path';
import test from 'node:test';
import {fileURLToPath} from 'node:url';

// The Effect rules of the desktop app (docs/contributing/clients-architecture.md, "State"), as
// wired in biome.json: each Grit rule has a source it must reject, one it must accept, and the
// folders it applies to. Fixtures are written under the app's real directories so the biome.json
// override globs match them exactly as they match product code.

const WORKSPACE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const BIOME = join(
  WORKSPACE_ROOT,
  'node_modules',
  '.bin',
  process.platform === 'win32' ? 'biome.cmd' : 'biome',
);
const SRC = 'apps/desktop/src';

function pluginDiagnostics(path) {
  const result = spawnSync(BIOME, ['lint', '--reporter=json', path], {
    cwd: WORKSPACE_ROOT,
    encoding: 'utf8',
  });
  assert.equal(result.error, undefined, result.error?.message);
  return JSON.parse(result.stdout).diagnostics.filter(
    diagnostic => diagnostic.category === 'plugin',
  );
}

async function lintIn(t, directory, source, name = 'sample.ts') {
  const parent = join(WORKSPACE_ROOT, SRC, directory);
  await mkdir(parent, {recursive: true});
  const fixture = await mkdtemp(join(parent, 'effect-fixture-'));
  t.after(() => rm(fixture, {recursive: true, force: true}));
  const path = join(fixture, name);
  await writeFile(path, source);
  return pluginDiagnostics(path);
}

/** Lint `source` as the designated entry file `file` (under the app's src), then restore it. */
async function lintAsEntry(t, file, source) {
  const path = join(WORKSPACE_ROOT, SRC, file);
  const original = await readFile(path, 'utf8').catch(() => undefined);
  t.after(() => (original === undefined ? rm(path, {force: true}) : writeFile(path, original)));
  await mkdir(dirname(path), {recursive: true});
  await writeFile(path, source);
  return pluginDiagnostics(path);
}

const REJECT = (diagnostics, count = 1) => assert.equal(diagnostics.length, count);
const ACCEPT = diagnostics => assert.deepEqual(diagnostics, []);

// --- effect-namespace-imports -----------------------------------------------------------------

const BARREL = {
  'a named import from the barrel': "import {Effect} from 'effect';\nexport const run = Effect;\n",
  'a namespace import from the barrel': "import * as E from 'effect';\nexport const run = E;\n",
  'a type import from the barrel':
    "import type {Effect} from 'effect';\nexport type Run = Effect<void>;\n",
  'a named import of a module member':
    "import {map, flatMap} from 'effect/Effect';\nexport const run = [map, flatMap];\n",
  'a single named import of a module member':
    "import {Option} from 'effect/Option';\nexport const run = Option;\n",
};
for (const [name, source] of Object.entries(BARREL)) {
  test(`effect-namespace-imports rejects ${name}`, async t => {
    REJECT(await lintIn(t, 'core', source));
  });
}
test('effect-namespace-imports accepts namespace subpath imports, type imports of them, and effect/Function by name', async t => {
  const source = [
    "import type * as Duration from 'effect/Duration';",
    "import * as Effect from 'effect/Effect';",
    "import {pipe} from 'effect/Function';",
    'export const run = pipe(Effect.void, Effect.asVoid);',
    'export type Wait = Duration.Duration;',
    '',
  ].join('\n');
  ACCEPT(await lintIn(t, 'core', source));
});
test('effect-namespace-imports covers every folder of the app, tests included', async t => {
  for (const [directory, name] of [
    ['state', 'sample.ts'],
    ['ui', 'sample.tsx'],
    ['main', 'sample.ts'],
    ['testing', 'sample.ts'],
    ['core', 'sample.test.ts'],
  ]) {
    REJECT(await lintIn(t, directory, BARREL['a named import from the barrel'], name));
  }
});
test('effect-namespace-imports leaves other packages alone', async t => {
  const fixture = await mkdtemp(
    join(WORKSPACE_ROOT, 'libs', 'display-names', 'src', 'effect-fixture-'),
  );
  t.after(() => rm(fixture, {recursive: true, force: true}));
  const path = join(fixture, 'sample.ts');
  await writeFile(path, BARREL['a named import from the barrel']);
  ACCEPT(pluginDiagnostics(path));
});

// --- effect-no-ambient-time-random ------------------------------------------------------------

const AMBIENT = {
  'Date.now': 'export const now = () => Date.now();\n',
  'new Date()': 'export const now = () => new Date();\n',
  'performance.now': 'export const now = () => performance.now();\n',
  'Math.random': 'export const roll = () => Math.random();\n',
  'crypto.randomUUID': 'export const id = () => crypto.randomUUID();\n',
  'globalThis.crypto.randomUUID': 'export const id = () => globalThis.crypto.randomUUID();\n',
  setTimeout: 'export const later = () => setTimeout(() => undefined, 1);\n',
  setInterval: 'export const every = () => setInterval(() => undefined, 1);\n',
  'an alias of setTimeout': 'export const later = setTimeout;\n',
  'globalThis.setTimeout': 'export const later = () => globalThis.setTimeout(() => 0, 1);\n',
};
for (const directory of ['core', 'state', 'ui']) {
  for (const [name, source] of Object.entries(AMBIENT)) {
    test(`effect-no-ambient-time-random: src/${directory} rejects ${name}`, async t => {
      const diagnostics = await lintIn(t, directory, source, 'sample.ts');
      // src/ui is also held to ui-no-timers, which flags a timer call a second time.
      if (directory === 'ui') assert.ok(diagnostics.length >= 1);
      else REJECT(diagnostics);
    });
  }
}
test('effect-no-ambient-time-random accepts a date built from a value', async t => {
  const source = 'export const at = (ms: number) => new Date(ms);\n';
  ACCEPT(await lintIn(t, 'core', source));
});
test('effect-no-ambient-time-random leaves src/main, src/testing and tests to their own rules', async t => {
  ACCEPT(await lintIn(t, 'main', AMBIENT['Date.now']));
  ACCEPT(await lintIn(t, 'testing', AMBIENT['Math.random']));
});

// --- effect-run-only-in-entries ---------------------------------------------------------------

const RUNNERS = {
  'Effect.runPromise':
    "import * as Effect from 'effect/Effect';\nexport const go = () => Effect.runPromise(Effect.void);\n",
  'Effect.runFork':
    "import * as Effect from 'effect/Effect';\nexport const go = () => Effect.runFork(Effect.void);\n",
  'Effect.runSync':
    "import * as Effect from 'effect/Effect';\nexport const go = () => Effect.runSync(Effect.void);\n",
  'Effect.runPromiseExit':
    "import * as Effect from 'effect/Effect';\nexport const go = () => Effect.runPromiseExit(Effect.void);\n",
  'a ManagedRuntime runFork':
    'export const go = (runtime: {runFork(x: unknown): void}) => runtime.runFork(1);\n',
};
for (const [name, source] of Object.entries(RUNNERS)) {
  for (const directory of ['core', 'ui', 'main']) {
    test(`effect-run-only-in-entries: src/${directory} rejects ${name}`, async t => {
      REJECT(await lintIn(t, directory, source));
    });
  }
}
test('effect-run-only-in-entries rejects a runner in a test, which uses it.effect', async t => {
  REJECT(await lintIn(t, 'core', RUNNERS['Effect.runPromise'], 'sample.test.ts'));
});
test('effect-run-only-in-entries accepts the designated entry files', async t => {
  ACCEPT(await lintAsEntry(t, 'main/index.ts', RUNNERS['Effect.runFork']));
  ACCEPT(await lintAsEntry(t, 'ui/main.tsx', RUNNERS['Effect.runFork']));
});
test('effect-run-only-in-entries accepts Stream and Effect members that only start with run', async t => {
  const source = [
    "import * as Stream from 'effect/Stream';",
    'export const drain = Stream.runDrain;',
    'export const collect = Stream.runCollect;',
    '',
  ].join('\n');
  ACCEPT(await lintIn(t, 'core', source));
});

// --- effect-core-no-promises ------------------------------------------------------------------

const PROMISES = {
  'an async function': 'export async function load() {\n  return 1;\n}\n',
  'an async generator function': 'export async function* load() {\n  yield 1;\n}\n',
  'an async function expression': 'export const load = async function () {\n  return 1;\n};\n',
  'an async arrow function': 'export const load = async () => 1;\n',
  'an async arrow function of one parameter': 'export const load = async (x: number) => x;\n',
  'an async method': 'export class Loader {\n  async load() {\n    return 1;\n  }\n}\n',
  'an async object method': 'export const loader = {\n  async load() {\n    return 1;\n  },\n};\n',
  await: 'export const load = (p: unknown) => (async () => await p)();\n',
  'a Promise return type': 'export const load = (): Promise<number> => undefined as never;\n',
  'an AbortController': 'export const stop = () => new AbortController();\n',
  'Promise.resolve': 'export const load = () => Promise.resolve(1);\n',
  'new Promise': 'export const load = () => new Promise(() => undefined);\n',
};
for (const [name, source] of Object.entries(PROMISES)) {
  test(`effect-core-no-promises rejects ${name}`, async t => {
    const diagnostics = await lintIn(t, 'core', source);
    assert.ok(diagnostics.length >= 1, `${name} must be rejected`);
  });
}
test('effect-core-no-promises accepts Effect.tryPromise, which wraps a Promise API', async t => {
  const source = [
    "import * as Effect from 'effect/Effect';",
    'export const read = (url: string) =>',
    '  Effect.tryPromise({try: () => fetch(url), catch: cause => cause});',
    '',
  ].join('\n');
  ACCEPT(await lintIn(t, 'core', source));
});
test('effect-core-no-promises leaves the other folders alone', async t => {
  ACCEPT(await lintIn(t, 'main', PROMISES['an async function']));
  ACCEPT(await lintIn(t, 'testing', PROMISES['Promise.resolve']));
});

// --- effect-tests-no-real-time ----------------------------------------------------------------

const REAL_TIME = {
  'Date.now': "it('x', () => Date.now());\n",
  'new Date()': "it('x', () => new Date());\n",
  setTimeout: "it('x', () => setTimeout(() => undefined, 1));\n",
  'it.live': "it.live('x', () => Effect.void);\n",
  'it.scopedLive': "it.scopedLive('x', () => Effect.void);\n",
  'it.flakyTest': 'it.flakyTest(Effect.void);\n',
  'vi.waitFor': "it('x', () => vi.waitFor(() => undefined));\n",
  'vi.useFakeTimers': "it('x', () => vi.useFakeTimers());\n",
  'expect.poll': "it('x', () => expect.poll(() => 1));\n",
  'a timeout option': "it.effect('x', () => Effect.void, {timeout: 5000});\n",
  'a retry option': "it.effect('x', () => Effect.void, {retry: 3});\n",
  'Math.random': "it('x', () => Math.random());\n",
};
for (const [name, source] of Object.entries(REAL_TIME)) {
  test(`effect-tests-no-real-time rejects ${name} in a test`, async t => {
    const diagnostics = await lintIn(t, 'core', source, 'sample.test.ts');
    assert.ok(diagnostics.length >= 1, `${name} must be rejected`);
  });
}
test('effect-tests-no-real-time accepts a test on TestClock', async t => {
  const source = [
    "import {it} from '@effect/vitest';",
    "import * as Effect from 'effect/Effect';",
    "import * as TestClock from 'effect/testing/TestClock';",
    "it.effect('x', () => Effect.gen(function* () { yield* TestClock.adjust('1 second'); }));",
    '',
  ].join('\n');
  ACCEPT(await lintIn(t, 'core', source, 'sample.test.ts'));
});
test('effect-tests-no-real-time applies to tests only', async t => {
  ACCEPT(await lintIn(t, 'main', REAL_TIME['it.live'], 'sample.ts'));
});
