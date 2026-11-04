import assert from 'node:assert/strict';
import {mkdir, mkdtemp, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import test from 'node:test';
import {fileURLToPath} from 'node:url';
import {
  readWorkspaceSources,
  undefinedCustomPropertyErrors,
} from './check_css_custom_properties.mjs';

const WORKSPACE_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

// Names that exercise the grammar: digits, dashes, underscores, a prefix of another name.
const NAMES = [
  '--a',
  '--space-3',
  '--seg-color',
  '--_private',
  '--series-12',
  '--x1-y-2',
  '--space',
];

const css = (path, text) => ({path, text});

test('a reference passes when a stylesheet or a TypeScript style defines it', () => {
  const fixture = [
    css('libs/ui/src/tokens.css', ':root { --space-3: 12px; --accent: red; }'),
    css('libs/ui/src/card.css', '.card { padding: var(--space-3); color: var(--accent); }'),
    css('libs/ui/src/meter.tsx', "export const s = {'--meter-color': 'red'};"),
    css('libs/ui/src/meter.css', '.meter { color: var(--meter-color); }'),
    css('libs/ui/src/layout.ts', "el.style.setProperty('--chrome-end-height', '4px');"),
    css('libs/ui/src/chrome.css', '.chrome { height: var(--chrome-end-height); }'),
    css('libs/ui/src/props.css', '@property --angle { syntax: "<angle>"; inherits: false; }'),
    css('libs/ui/src/spin.css', '.spin { rotate: var(--angle); }'),
  ];
  assert.deepEqual(undefinedCustomPropertyErrors(fixture), []);
});

test('an undefined reference fails naming its file, line and property', () => {
  const fixture = [
    css('libs/ui/src/tokens.css', ':root { --space-3: 12px; }'),
    css(
      'features/toast/src/toast.css',
      '.toast {\n  gap: var(--space-3);\n  top: var(--titlebar);\n}\n',
    ),
  ];
  const errors = undefinedCustomPropertyErrors(fixture);
  assert.equal(errors.length, 1);
  assert.match(errors[0], /features\/toast\/src\/toast\.css:3: var\(--titlebar\)/);
  assert.match(errors[0], /fallback/);
});

test('a reference with a fallback passes whether or not the property is defined', () => {
  const fixture = [
    css('a.css', '.a { top: var(--missing, 0); left: var( --also-missing , var(--x, 1px)); }'),
    css('b.css', '.b { top: var(--missing,); }'),
  ];
  assert.deepEqual(undefinedCustomPropertyErrors(fixture), []);
});

test('a fallback does not excuse a reference nested inside it', () => {
  const errors = undefinedCustomPropertyErrors([css('a.css', '.a { top: var(--m, var(--n)); }')]);
  assert.equal(errors.length, 1);
  assert.match(errors[0], /a\.css:1: var\(--n\)/);
});

test('a reference to a computed name is not checked and a computed definition is not seen', () => {
  const fixture = [
    css('a.tsx', `const s = {"--seg-color": \`var(--series-\${n})\`, gap: \`var(--\${gap})\`};`),
    css('b.ts', `el.style.setProperty(\`--dyn-\${n}\`, "1"); const t = \`var(--dyn-1)\`;`),
  ];
  const errors = undefinedCustomPropertyErrors(fixture);
  assert.equal(errors.length, 1);
  assert.match(errors[0], /b\.ts:1: var\(--dyn-1\)/);
});

test('comments, tests and command-line flags define nothing', () => {
  const fixture = [
    css('a.css', '/* --ghost: 1; */ .a { top: var(--ghost); }'),
    css('a.test.tsx', "const s = {'--ghost': '1'};"),
    css('flags.ts', "export const FLAGS = ['--ghost', '--ghost: not a style'];"),
  ];
  const errors = undefinedCustomPropertyErrors(fixture);
  assert.equal(errors.length, 1);
  assert.match(errors[0], /a\.css:1: var\(--ghost\)/);
});

test('a reference in a comment, a test or a non-source file is not reported', () => {
  const fixture = [
    css('a.css', '/* var(--ghost) */ .a { top: 0; }'),
    css('a.test.ts', "expect(el).toHaveStyle('top: var(--ghost)');"),
    css('rules.grit', 'var(--ghost)'),
  ];
  assert.deepEqual(undefinedCustomPropertyErrors(fixture), []);
});

test('line numbers count the lines inside a multi-line comment', () => {
  const [error] = undefinedCustomPropertyErrors([
    css('a.css', '/* one\n two\n three */\n.a {\n  top: var(--ghost);\n}\n'),
  ]);
  assert.match(error, /a\.css:5: /);
});

for (const name of NAMES) {
  test(`every way of setting or referencing ${name} agrees`, () => {
    const references = [css('use.css', `.u { top: var(${name}); }`)];
    const definitions = {
      'a stylesheet declaration': css('def.css', `:root { ${name}: 1px; }`),
      'a style-object key': css('def.tsx', `export const s = {'${name}': 1};`),
      'a double-quoted key': css('def.ts', `export const s = {"${name}": 1};`),
      setProperty: css('def.ts', `el.style.setProperty('${name}', '1');`),
      '@property': css('def.css', `@property ${name} { inherits: true; }`),
    };
    const errors = undefinedCustomPropertyErrors(references);
    assert.equal(errors.length, 1);
    assert.ok(errors[0].includes(`var(${name})`));
    for (const [how, file] of Object.entries(definitions)) {
      assert.deepEqual(undefinedCustomPropertyErrors([...references, file]), [], how);
    }
    // A longer name that starts with this one does not define it.
    const longer = css('def.css', `:root { ${name}-more: 1px; }`);
    assert.equal(undefinedCustomPropertyErrors([...references, longer]).length, 1);
  });
}

test('the workspace scan reads stylesheets and sources under the scan roots, not build output', async t => {
  const root = await mkdtemp(join(tmpdir(), 'vibesys-css-properties-'));
  t.after(() => rm(root, {recursive: true, force: true}));
  const files = {
    'pnpm-workspace.yaml': 'packages:\n  - "libs/*"\n',
    'libs/ui/package.json': JSON.stringify({name: '@vibesys/ui'}),
    'libs/ui/src/tokens.css': ':root { --space-3: 12px; }',
    'libs/ui/src/card.css': '.card { top: var(--space-3); left: var(--titlebar); }',
    'libs/ui/src/inline.tsx': "export const s = {'--meter-color': 'red'};",
    'libs/ui/src/nested/deeper.css': '.d { color: var(--meter-color); }',
    'libs/ui/dist/stale.css': '.stale { top: var(--from-build-output); }',
  };
  for (const [path, text] of Object.entries(files)) {
    await mkdir(dirname(join(root, path)), {recursive: true});
    await writeFile(join(root, path), text);
  }
  const sources = readWorkspaceSources(root);
  assert.deepEqual(sources.map(({path}) => path).sort(), [
    'libs/ui/src/card.css',
    'libs/ui/src/inline.tsx',
    'libs/ui/src/nested/deeper.css',
    'libs/ui/src/tokens.css',
  ]);
  const errors = undefinedCustomPropertyErrors(sources);
  assert.equal(errors.length, 1);
  assert.match(errors[0], /libs\/ui\/src\/card\.css:1: var\(--titlebar\)/);
});

// The real tree: it must pass, and deleting any token's definitions must fail naming that token.
const REAL_SOURCES = readWorkspaceSources(WORKSPACE_ROOT);
const DECLARATION = (name, flags) =>
  new RegExp(String.raw`(?<![\w-])${name}(?=\s*:)|(?<=['"\`])${name}(?=['"\`]\s*:)`, flags);

/** Every name a shipped file references without a fallback. */
function referencedNames(files) {
  const names = new Set();
  for (const {path, text} of files) {
    if (/\.test\.[cm]?tsx?$/.test(path) || !/\.(?:css|[cm]?tsx?)$/.test(path)) continue;
    for (const match of text.matchAll(/var\(\s*(--[A-Za-z_][\w-]*)\s*\)/g)) names.add(match[1]);
  }
  return [...names].sort();
}

test('the real tree has no undefined custom property reference', () => {
  assert.deepEqual(undefinedCustomPropertyErrors(REAL_SOURCES), []);
});

test('deleting a real token definition fails the check and names the token', () => {
  const names = referencedNames(REAL_SOURCES);
  assert.ok(names.length > 50, 'the real tree references many tokens, or the scan found nothing');
  // An even spread of the sorted names (the check costs a full scan each), each with its
  // definitions renamed in every file that sets it.
  const stride = Math.ceil(names.length / 16);
  for (const name of names.filter((_, index) => index % stride === 0)) {
    const declaration = DECLARATION(name.replaceAll('-', String.raw`\-`), 'g');
    const mutated = REAL_SOURCES.map(file =>
      file.text.includes(name)
        ? {...file, text: file.text.replaceAll(declaration, `${name}-deleted`)}
        : file,
    );
    const errors = undefinedCustomPropertyErrors(mutated);
    assert.ok(
      errors.some(error => error.includes(`var(${name})`)),
      `${name}: deleting its definition did not fail the check`,
    );
  }
});
