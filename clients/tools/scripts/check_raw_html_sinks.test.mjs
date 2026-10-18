import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdtemp, rm, writeFile} from 'node:fs/promises';
import {dirname, join, resolve} from 'node:path';
import test from 'node:test';
import {fileURLToPath} from 'node:url';

const WORKSPACE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const BIOME = join(
  WORKSPACE_ROOT,
  'node_modules',
  '.bin',
  process.platform === 'win32' ? 'biome.cmd' : 'biome',
);

function pluginDiagnostics(path) {
  const result = spawnSync(BIOME, ['lint', '--reporter=json', path], {
    cwd: WORKSPACE_ROOT,
    encoding: 'utf8',
  });
  assert.equal(result.error, undefined, result.error?.message);
  const diagnostics = JSON.parse(result.stdout).diagnostics;
  assert.deepEqual(
    diagnostics.filter(diagnostic => diagnostic.category !== 'plugin'),
    [],
  );
  return diagnostics.filter(diagnostic => diagnostic.category === 'plugin');
}

function assertRawHtmlDiagnostics(diagnostics, count) {
  assert.equal(diagnostics.length, count);
  assert.deepEqual(
    diagnostics.map(diagnostic => diagnostic.location.start.line),
    Array.from({length: count}, (_, index) => index + 1),
  );
  for (const diagnostic of diagnostics) {
    assert.match(JSON.stringify(diagnostic), /gateway CSP contract/);
  }
}

// Fixtures live inside real package paths so they exercise the same Biome configuration as
// application code, including the overlapping UI and web-specific plugin overrides.
async function lintFixture(t, parent, source, name = 'sample.ts') {
  const fixture = await mkdtemp(join(WORKSPACE_ROOT, parent, 'raw-html-fixture-'));
  t.after(() => rm(fixture, {recursive: true, force: true}));
  const path = join(fixture, name);
  await writeFile(path, source);
  return pluginDiagnostics(path);
}

const RAW_HTML_SINKS = {
  'innerHTML assignment': 'element.innerHTML = text;\n',
  'outerHTML assignment': 'element.outerHTML = text;\n',
  insertAdjacentHTML: "element.insertAdjacentHTML('beforeend', text);\n",
  'document.write': 'document.write(text);\n',
  'window.document.write': 'window.document.write(text);\n',
  'globalThis.document.write': 'globalThis.document.write(text);\n',
};

const COMPOUND_ASSIGNMENTS = [
  'element.innerHTML += text;',
  'element.innerHTML &&= text;',
  'element.innerHTML ||= text;',
  'element.innerHTML ??= text;',
  'element.outerHTML += text;',
  'element.outerHTML &&= text;',
  'element.outerHTML ||= text;',
  'element.outerHTML ??= text;',
];

const COMPUTED_ASSIGNMENTS = [
  'element["innerHTML"] = text;',
  "element['innerHTML'] += text;",
  'element["innerHTML"] &&= text;',
  "element['innerHTML'] ||= text;",
  'element["innerHTML"] ??= text;',
  "element['outerHTML'] = text;",
  'element["outerHTML"] += text;',
  "element['outerHTML'] &&= text;",
  'element["outerHTML"] ||= text;',
  "element['outerHTML'] ??= text;",
];

const OPTIONAL_AND_COMPUTED_CALLS = [
  "element.insertAdjacentHTML?.('beforeend', text);",
  "element?.insertAdjacentHTML('beforeend', text);",
  "element?.insertAdjacentHTML?.('beforeend', text);",
  "element['insertAdjacentHTML']('beforeend', text);",
  'element["insertAdjacentHTML"]?.(\'beforeend\', text);',
  "element?.['insertAdjacentHTML']?.('beforeend', text);",
  'document.write?.(text);',
  'document?.write(text);',
  'document?.write?.(text);',
  "document['write'](text);",
  'document?.["write"]?.(text);',
  "window.document['write']?.(text);",
  'window.document?.write(text);',
  'window.document?.["write"]?.(text);',
  'window?.document.write?.(text);',
  "window?.document?.['write'](text);",
  "globalThis.document['write']?.(text);",
  'globalThis.document?.write(text);',
  "globalThis.document?.['write']?.(text);",
  'globalThis?.document.write?.(text);',
  "globalThis?.document?.['write'](text);",
];

for (const [name, source] of Object.entries(RAW_HTML_SINKS)) {
  test(`the client gate rejects ${name}`, async t => {
    assertRawHtmlDiagnostics(await lintFixture(t, 'apps/desktop-shell/src', source), 1);
  });
}

for (const [name, sources] of [
  ['compound assignments', COMPOUND_ASSIGNMENTS],
  ['computed assignments', COMPUTED_ASSIGNMENTS],
  ['optional and computed calls', OPTIONAL_AND_COMPUTED_CALLS],
]) {
  test(`the client gate rejects ${name}`, async t => {
    const diagnostics = await lintFixture(t, 'apps/desktop-shell/src', `${sources.join('\n')}\n`);
    assertRawHtmlDiagnostics(diagnostics, sources.length);
  });
}

test('CommonJS TypeScript preload files are covered by the raw HTML gate', async t => {
  const diagnostics = await lintFixture(
    t,
    'apps/desktop-main/src/preload',
    RAW_HTML_SINKS['document.write'],
    'sample.cts',
  );
  assertRawHtmlDiagnostics(diagnostics, 1);
});

for (const parent of ['features/run-page/src', 'libs/run-state/src']) {
  test(`${parent} is covered by the raw HTML gate`, async t => {
    assertRawHtmlDiagnostics(
      await lintFixture(t, parent, RAW_HTML_SINKS['innerHTML assignment']),
      1,
    );
  });
}

test('text and structured DOM APIs remain available', async t => {
  const source = "element.textContent = text;\nelement.setAttribute('data-status', text);\n";
  assert.deepEqual(await lintFixture(t, 'apps/desktop-shell/src', source), []);
});
