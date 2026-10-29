import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdir, mkdtemp, readFile, realpath, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import test from 'node:test';
import {fileURLToPath} from 'node:url';
import {STRICT_LINT_PLUGINS} from './check_package_graph.mjs';

// The Grit rules of the design system (docs/contributing/clients-architecture.md, "Design system
// enforcement"): S3 `strict-no-raw-elements`, S4 `strict-no-raw-values`, and S8 for `@vibesys/ui`
// (`strict-no-raw-values` on its TypeScript, `ui-no-raw-css-values` on its stylesheets). The strict
// rules run on a fixture package under a Biome configuration built from the real override; the S8
// rules run through the real configuration on fixtures inside `libs/ui/src`.

const WORKSPACE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
const BIOME = join(WORKSPACE_ROOT, 'node_modules', '.bin', 'biome');

async function strictConfig(directory) {
  const real = JSON.parse(await readFile(join(WORKSPACE_ROOT, 'biome.json'), 'utf8'));
  const override = real.overrides.find(({plugins = []}) =>
    plugins.includes(STRICT_LINT_PLUGINS[0]),
  );
  assert.ok(override, 'biome.json has no strict override');
  assert.deepEqual(override.plugins, STRICT_LINT_PLUGINS);
  await writeFile(
    join(directory, 'biome.json'),
    JSON.stringify({
      linter: {enabled: true, rules: {recommended: false}},
      overrides: [
        {
          includes: [
            'strict-package/src/**/*.ts',
            'strict-package/src/**/*.tsx',
            '!**/*.test.ts',
            '!**/*.test.tsx',
          ],
          plugins: override.plugins.map(plugin => join(WORKSPACE_ROOT, plugin)),
        },
      ],
    }),
  );
}

function pluginDiagnostics(args, cwd) {
  const result = spawnSync(BIOME, ['lint', '--reporter=json', ...args], {cwd, encoding: 'utf8'});
  assert.equal(result.error, undefined, result.error?.message);
  return JSON.parse(result.stdout).diagnostics.filter(
    diagnostic => diagnostic.category === 'plugin',
  );
}

/** The diagnostics for `source` as the file `path` of a fresh fixture package. */
async function lintStrict(t, source, path = 'strict-package/src/screen.tsx') {
  // The real path: Biome matches `includes` against paths with symlinks resolved.
  const directory = await realpath(await mkdtemp(join(tmpdir(), 'vibesys-strict-lint-')));
  t.after(() => rm(directory, {recursive: true, force: true}));
  await strictConfig(directory);
  await mkdir(dirname(join(directory, path)), {recursive: true});
  await writeFile(join(directory, path), source);
  return pluginDiagnostics([`--config-path=${directory}`, path], directory);
}

/** The diagnostics for `source` in a fresh fixture directory of the real workspace. */
async function lintInWorkspace(t, parent, name, source) {
  const fixture = await mkdtemp(join(WORKSPACE_ROOT, parent, 'strict-fixture-'));
  t.after(() => rm(fixture, {recursive: true, force: true}));
  const path = join(fixture, name);
  await mkdir(dirname(path), {recursive: true});
  await writeFile(path, source);
  return pluginDiagnostics([path], WORKSPACE_ROOT);
}

/** A template placeholder `${name}`, spelled so a plain string holding it does not trip lint. */
const hole = name => `$\{${name}}`;
const lines = diagnostics => diagnostics.map(diagnostic => diagnostic.location.start.line);
const oneStatementPerLine = statements => `${statements.join('\n')}\n`;
const everyLine = count => Array.from({length: count}, (_, index) => index + 1);

// S3: raw elements and style props.

const RAW_ELEMENTS = {
  button: 'Button',
  a: 'Button',
  input: 'Field',
  textarea: 'Field',
  label: 'Field',
  select: 'Select',
  dialog: 'Dialog',
  details: 'Timeline',
  summary: 'Timeline',
  form: 'Field',
  table: 'Table',
  svg: 'Icon',
  pre: 'LogPane',
};

for (const [tag, component] of Object.entries(RAW_ELEMENTS)) {
  test(`strict-no-raw-elements rejects <${tag}> and says to use ${component}`, async t => {
    const source = oneStatementPerLine([
      `export const open = <${tag}>text</${tag}>;`,
      `export const closed = <${tag} />;`,
      `export const withProps = <${tag} id="x" onClick={f}>text</${tag}>;`,
    ]);
    const diagnostics = await lintStrict(t, source);
    assert.deepEqual(lines(diagnostics), [1, 2, 3]);
    for (const diagnostic of diagnostics) {
      assert.match(JSON.stringify(diagnostic), /strict-no-raw-elements \(S3\)/);
      assert.match(JSON.stringify(diagnostic), new RegExp(`\`<?${component}`));
    }
  });
}

for (const prop of ['style', 'className', 'dangerouslySetInnerHTML']) {
  test(`strict-no-raw-elements rejects the ${prop} prop on any element`, async t => {
    const source = oneStatementPerLine([
      `export const onDiv = <div ${prop}={x}>text</div>;`,
      `export const onSpan = <span ${prop}="x" />;`,
      `export const onComponent = <Card ${prop}={x} />;`,
      `export const bare = <Card ${prop} />;`,
    ]);
    const diagnostics = await lintStrict(t, source);
    assert.deepEqual(lines(diagnostics), [1, 2, 3, 4]);
    assert.match(JSON.stringify(diagnostics[0]), /strict-no-raw-elements \(S3\).*@vibesys\/ui/);
  });
}

test('strict-no-raw-elements reports each offending prop and element once', async t => {
  const source = '<div style={a} className={b}><button className="c" /></div>;\n';
  assert.equal((await lintStrict(t, source)).length, 4);
});

test('strict-no-raw-elements leaves grouping elements, components and data attributes alone', async t => {
  const source = oneStatementPerLine([
    'export const grouping = <div><span>text</span></div>;',
    'export const components = <Stack gap="4"><Button onPress={f} /><Text tone="muted" /></Stack>;',
    'export const data = <div data-style="a" data-class-name="b" aria-label="c" />;',
    'export const fragment = <>text</>;',
    'export const lookalikes = <Button><Anchor /><Selection /><Preview /></Button>;',
  ]);
  assert.deepEqual(await lintStrict(t, source), []);
});

test('the S3 and S4 rules do not apply to tests or paths outside the override', async t => {
  const raw = 'export const a = <button style={{color: "#fff"}} />;\n';
  assert.deepEqual(await lintStrict(t, raw, 'strict-package/src/screen.test.tsx'), []);
  assert.deepEqual(await lintStrict(t, raw, 'other-package/src/screen.tsx'), []);
  assert.notEqual((await lintStrict(t, raw)).length, 0);
});

test('the workspace override covers new feature and app packages without a marker', async t => {
  const raw = 'export const a = <button style={{color: "#fff"}} />;\n';
  for (const folder of ['features', 'apps']) {
    const diagnostics = await lintInWorkspace(t, folder, 'src/screen.tsx', raw);
    assert.equal(diagnostics.length, 3);
  }
});

// S4: raw colors and lengths in strings.

const LENGTHS = ['0.5', '1', '4', '12', '100', '1.5', '.5'].flatMap(number =>
  ['px', 'rem', 'em', 'vh', 'vw'].map(unit => `${number}${unit}`),
);
const RAW_VALUES = [
  ...LENGTHS,
  'calc(100vh - 4px)',
  'min(680px, 92vw)',
  '0 4px 12px',
  '-4px',
  'margin: 8px;',
  '#fff',
  '#FFFF',
  '#a1b2c3',
  '#A1B2C3D4',
  'border: 1px solid #ccc',
  ...['rgb(', 'rgba(', 'hsl(', 'hsla(', 'oklch(', 'color-mix('].map(fn => `${fn}0 0 0)`),
  'color-mix(in srgb, var(--accent) 12%, var(--panel))',
];
const TOKEN_VALUES = [
  'var(--space-4)',
  'space-4',
  '4',
  'pixel',
  'v1px',
  'mrgb(1)',
  '#section',
  '#12345',
  '1fr',
  '100%',
  '0',
  'step2 0 1.5',
  'a.5em',
];

test('strict-no-raw-values rejects every raw color and length in a string literal', async t => {
  const source = oneStatementPerLine(
    RAW_VALUES.map((value, index) => `export const v${index} = ${JSON.stringify(value)};`),
  );
  const diagnostics = await lintStrict(t, source);
  assert.deepEqual(lines(diagnostics), everyLine(RAW_VALUES.length));
  assert.match(JSON.stringify(diagnostics[0]), /strict-no-raw-values \(S4, S8\).*SpaceToken/);
});

test('strict-no-raw-values rejects raw values in template literals and JSX strings', async t => {
  const source = oneStatementPerLine([
    'export const a = `padding: 12px`;',
    `export const b = \`${hole('x')} 12px ${hole('y')}\`;`,
    `export const c = \`a ${hole('x')} #fff\`;`,
    'export const d = `\n  line 1.5em\n`;',
    'export const e = <Card title="#abc" />;',
    "export const f = <Card title='4px' />;",
  ]);
  const diagnostics = await lintStrict(t, source);
  assert.deepEqual(lines(diagnostics), [1, 2, 3, 4, 7, 8]);
});

test('strict-no-raw-values leaves token names and values built from tokens alone', async t => {
  const source = oneStatementPerLine([
    ...TOKEN_VALUES.map((value, index) => `export const t${index} = ${JSON.stringify(value)};`),
    `export const spaced = \`${hole('gap')}px\`;`,
    `export const mixed = \`var(--space-${hole('n')})\`;`,
  ]);
  assert.deepEqual(await lintStrict(t, source), []);
});

// S8: @vibesys/ui itself, through the real configuration.

test('strict-no-raw-values covers libs/ui/src outside the token table and tests', async t => {
  const source = 'export const bad = "12px";\nexport const worse = "#fff";\n';
  const covered = await lintInWorkspace(t, 'libs/ui/src', 'raw.ts', source);
  assert.deepEqual(lines(covered), [1, 2]);
  assert.deepEqual(await lintInWorkspace(t, 'libs/ui/src/tokens', 'table.ts', source), []);
  assert.deepEqual(await lintInWorkspace(t, 'libs/ui/src', 'raw.test.ts', source), []);
});

test('the raw element rules do not apply to libs/ui, whose components render the elements', async t => {
  const source = 'export const b = <button className="primary" />;\n';
  assert.deepEqual(await lintInWorkspace(t, 'libs/ui/src', 'raw.tsx', source), []);
});

test('ui-no-raw-css-values rejects raw colors and lengths in libs/ui stylesheets', async t => {
  const css = [
    '.a { padding: 4px 10px; }',
    '.b { color: #fff; }',
    '.c { background: rgb(0 0 0 / 0.5); }',
    '.d { width: min(680px, 92vw); }',
    ':root { --gap: 12px; }',
    '.e { box-shadow: 0 1px 2px color-mix(in srgb, red, blue); }',
    '.f { font: 13px/1.45 sans-serif; }',
    '.g { margin: -4px; }',
  ];
  const diagnostics = await lintInWorkspace(t, 'libs/ui/src', 'raw.css', `${css.join('\n')}\n`);
  assert.deepEqual(lines(diagnostics), everyLine(css.length));
  assert.match(JSON.stringify(diagnostics[0]), /ui-no-raw-css-values \(S8\).*var\(--space-3\)/);
});

test('ui-no-raw-css-values leaves declarations that read the token table alone', async t => {
  const css = [
    '.a { padding: var(--space-2) var(--space-4); }',
    '.b { color: var(--text); background: var(--panel-2); }',
    '.c { width: min(var(--content), 100%); border-radius: var(--radius); }',
    '.d { margin: 0; flex: 1 1 0; opacity: 0.55; line-height: 1.45; }',
    ':root { --local: var(--space-3); }',
    '.e { transition: opacity var(--duration-fast) var(--easing); }',
  ];
  const diagnostics = await lintInWorkspace(
    t,
    'libs/ui/src',
    'tokens-only.css',
    `${css.join('\n')}\n`,
  );
  assert.deepEqual(diagnostics, []);
});
