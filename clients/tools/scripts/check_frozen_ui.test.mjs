import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {mkdir, mkdtemp, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname, join} from 'node:path';
import test from 'node:test';
import {fileURLToPath} from 'node:url';
import {
  colorLiteralCount,
  exportedNames,
  FROZEN_UI_PACKAGES,
  frozenUiErrors,
  interactiveElementCount,
} from './check_frozen_ui.mjs';

// The checker is a function of two trees (path -> text), so every case is a synthetic base tree
// and a head tree that differs from it; no Git is involved. The base holds every frozen package
// with one UI file, so the cases mutate a copy of it.

const SCRIPT = join(dirname(fileURLToPath(import.meta.url)), 'check_frozen_ui.mjs');
const UI_FILE = 'view.tsx';
const CLEAN_VIEW = 'export const View = () => <div>text</div>;\n';

/** The first UI path of a package as a directory to put files in, `src/renderer/` for a feature. */
function uiDirectory(directory) {
  const [first] = FROZEN_UI_PACKAGES[directory];
  return first.endsWith('/') ? `${directory}/${first}` : `${directory}/${dirname(first)}/`;
}

function manifestOf(directory) {
  const isFeature = directory.startsWith('features/');
  return JSON.stringify({
    name: `@vibesys/${directory}`,
    exports: isFeature ? {'./renderer': {'@vibesys/source': './src/renderer/index.ts'}} : undefined,
  });
}

function baseTree() {
  const tree = new Map();
  for (const directory of Object.keys(FROZEN_UI_PACKAGES)) {
    tree.set(`${directory}/package.json`, manifestOf(directory));
    tree.set(`${uiDirectory(directory)}${UI_FILE}`, CLEAN_VIEW);
    if (directory.startsWith('features/')) {
      tree.set(`${directory}/src/renderer/index.ts`, "export {View} from './view.js';\n");
    }
  }
  return tree;
}

const CAMPAIGN = 'features/campaign';
const RENDERER = `${CAMPAIGN}/src/renderer`;

/** The errors of a head that is the base tree changed by `mutate`. */
function errorsAfter(mutate) {
  const head = baseTree();
  mutate(head);
  return frozenUiErrors(baseTree(), head);
}

test('a head identical to its base reports nothing', () => {
  assert.deepEqual(frozenUiErrors(baseTree(), baseTree()), []);
});

// New files.

test('every frozen package refuses a new UI file, naming the file and the fix', () => {
  for (const directory of Object.keys(FROZEN_UI_PACKAGES)) {
    const path = `${uiDirectory(directory)}fresh.tsx`;
    const errors = errorsAfter(head => head.set(path, CLEAN_VIEW));
    assert.equal(errors.length, 1, errors.join('\n'));
    assert.match(errors[0], new RegExp(`^frozen-ui: ${path}: .*\\(F1\\).*@vibesys/ui`));
  }
});

test('a new CSS or TypeScript file in the UI paths is refused too', () => {
  for (const name of ['fresh.css', 'fresh.ts', 'nested/deep/fresh.tsx']) {
    const errors = errorsAfter(head => head.set(`${RENDERER}/${name}`, ''));
    assert.equal(errors.length, 1, `${name}: ${errors.join('\n')}`);
  }
});

test('new tests, new files outside the UI paths and non-source files are allowed', () => {
  const errors = errorsAfter(head => {
    head.set(`${RENDERER}/view.test.tsx`, "import './view.js';\n");
    head.set(`${RENDERER}/view.dom.test.tsx`, '');
    head.set(`${CAMPAIGN}/src/state/reducer.ts`, 'export const reduce = 1;\n');
    head.set('apps/desktop-renderer/src/main.tsx', "import './wiring/stores.js';\n");
    head.set('apps/desktop-renderer/src/shell/sidebar.tsx', CLEAN_VIEW);
    head.set(`${RENDERER}/notes.md`, 'text');
  });
  assert.deepEqual(errors, []);
});

test('the app UI paths are the web app and the welcome view only', () => {
  const refused = [
    'apps/web/src/app/panel.tsx',
    'apps/web/src/styles.css',
    'apps/desktop-renderer/src/ui/panel.tsx',
    'apps/desktop-renderer/src/welcome.css',
  ];
  for (const path of refused) {
    assert.equal(errorsAfter(head => head.set(path, '')).length, 1, path);
  }
  const allowed = [
    'apps/web/src/platform/backend.ts',
    'apps/desktop-renderer/src/wiring/stores.ts',
  ];
  for (const path of allowed) {
    assert.deepEqual(
      errorsAfter(head => head.set(path, '')),
      [],
      path,
    );
  }
});

// Growth of a file's raw controls and colors.

const CONTROLS = {
  button: '<button type="button">go</button>',
  anchor: '<a href="/x">go</a>',
  'self-closing input': '<input value={v} />',
  select: '<select />',
  textarea: '<textarea />',
  label: '<label>name</label>',
  dialog: '<dialog />',
  details: '<details />',
  summary: '<summary />',
  form: '<form />',
};

for (const [name, element] of Object.entries(CONTROLS)) {
  test(`a fix may not add a raw ${name}`, () => {
    const path = `${RENDERER}/${UI_FILE}`;
    const errors = errorsAfter(head =>
      head.set(path, `export const View = () => <div>${element}</div>;\n`),
    );
    assert.equal(errors.length, 1, errors.join('\n'));
    assert.match(
      errors[0],
      new RegExp(`^frozen-ui: ${path}: raises its count of JSX interactive elements`),
    );
    assert.match(errors[0], /from 0 to 1 \(F1\).*@vibesys\/ui/);
  });
}

const COLORS = [
  '#fff',
  '#FFFF',
  '#a1b2c3',
  '#a1b2c3d4',
  'rgb(0 0 0)',
  'rgba(0, 0, 0, 0.5)',
  'hsl(0 0% 0%)',
  'hsla(0, 0%, 0%, 1)',
  'oklch(0.5 0.1 200)',
  'color-mix(in srgb, red, blue)',
];

for (const color of COLORS) {
  test(`a fix may not add the color ${color} to a component or a stylesheet`, () => {
    const tsx = `${RENDERER}/${UI_FILE}`;
    const css = 'features/campaign/src/renderer/view.css';
    const base = baseTree();
    base.set(css, '.a { color: var(--text); }\n');
    for (const [path, grown] of [
      [tsx, `export const View = () => <div style={{color: '${color}'}} />;\n`],
      [css, `.a { color: ${color}; }\n`],
    ]) {
      const head = new Map(base);
      head.set(path, grown);
      const errors = frozenUiErrors(base, head);
      assert.equal(errors.length, 1, `${path}: ${errors.join('\n')}`);
      assert.match(
        errors[0],
        new RegExp(
          `^frozen-ui: ${path}: raises its count of raw color literals from 0 to 1 \\(F1\\)`,
        ),
      );
    }
  });
}

test('a fix may keep, move or remove raw controls and colors', () => {
  const dirty = "export const View = () => <button style={{color: '#fff'}}>x</button>;\n";
  const base = baseTree();
  base.set(`${RENDERER}/${UI_FILE}`, dirty);
  const same = new Map(base);
  const fewer = new Map(base);
  fewer.set(`${RENDERER}/${UI_FILE}`, CLEAN_VIEW);
  const reworded = new Map(base);
  reworded.set(`${RENDERER}/${UI_FILE}`, dirty.replace('x', 'a longer label'));
  for (const head of [same, fewer, reworded]) assert.deepEqual(frozenUiErrors(base, head), []);
});

test('counts are per file, so moving a control into another file is a raise', () => {
  const base = baseTree();
  base.set(`${RENDERER}/a.tsx`, 'export const A = () => <button />;\n');
  base.set(`${RENDERER}/b.tsx`, CLEAN_VIEW);
  const head = new Map(base);
  head.set(`${RENDERER}/a.tsx`, CLEAN_VIEW);
  head.set(`${RENDERER}/b.tsx`, 'export const B = () => <button />;\n');
  const errors = frozenUiErrors(base, head);
  assert.equal(errors.length, 1);
  assert.ok(errors[0].includes(`${RENDERER}/b.tsx`), errors[0]);
});

test('components, data attributes and look-alike text are not counted', () => {
  const text = [
    'export const A = () => <Button><Anchor /><Selection /></Button>;',
    'export const B = () => <div data-button="a" aria-label="hash">a &#123; &#x1f600;</div>;',
    "export const C = 'button, a, input #12345 #zzzzzz rgbx(1) brgb(2)';",
  ].join('\n');
  assert.equal(interactiveElementCount(text, 'x.tsx'), 0);
  assert.equal(colorLiteralCount(text), 0);
});

test('counting finds nested, conditional and mapped elements', () => {
  const text =
    'export const A = () => <div>{items.map(i => <a key={i}>{ok ? <button /> : <input />}</a>)}</div>;\n';
  assert.equal(interactiveElementCount(text, 'x.tsx'), 3);
  assert.equal(interactiveElementCount(text, 'x.ts'), 0);
});

// Exports of the renderer entry.

const ENTRY = `${RENDERER}/index.ts`;

const EXPORT_GROWTH = {
  'a declaration': "export {View} from './view.js';\nexport const fresh = 1;\n",
  'a function': "export {View} from './view.js';\nexport function fresh() {}\n",
  'a type': "export {View} from './view.js';\nexport type Fresh = string;\n",
  'an interface': "export {View} from './view.js';\nexport interface Fresh {}\n",
  'a class': "export {View} from './view.js';\nexport class Fresh {}\n",
  'a named re-export': "export {View, Other as Fresh} from './view.js';\n",
  'a default export': "export {View} from './view.js';\nexport default function Page() {}\n",
  'a namespace re-export': "export {View} from './view.js';\nexport * as Fresh from './view.js';\n",
};

for (const [name, entry] of Object.entries(EXPORT_GROWTH)) {
  test(`the ./renderer entry may not gain ${name}`, () => {
    const errors = errorsAfter(head => head.set(ENTRY, entry));
    assert.equal(errors.length, 1, errors.join('\n'));
    assert.match(
      errors[0],
      new RegExp(`^frozen-ui: ${CAMPAIGN}/package\\.json: exports\\["\\./renderer"\\] gains`),
    );
    assert.match(errors[0], /\(F1\)/);
  });
}

test('the ./renderer entry may not gain a name through export *', () => {
  const base = baseTree();
  base.set(ENTRY, "export * from './views/index.js';\n");
  base.set(`${RENDERER}/views/index.ts`, "export * from './panel.js';\nexport const Old = 1;\n");
  base.set(`${RENDERER}/views/panel.ts`, 'export const Panel = 1;\n');
  const head = new Map(base);
  head.set(`${RENDERER}/views/panel.ts`, 'export const Panel = 1;\nexport const Fresh = 2;\n');
  const errors = frozenUiErrors(base, head);
  assert.equal(errors.length, 1, errors.join('\n'));
  assert.match(errors[0], /gains `Fresh`/);
});

test('the ./renderer entry may lose, keep or reorder its exports', () => {
  const base = baseTree();
  base.set(ENTRY, "export {View, Other} from './view.js';\nexport const A = 1;\n");
  for (const entry of [
    "export {View, Other} from './view.js';\nexport const A = 1;\n",
    "export const A = 1;\nexport {Other, View} from './view.js';\n",
    "export {View} from './view.js';\n",
    '',
  ]) {
    const head = new Map(base);
    head.set(ENTRY, entry);
    assert.deepEqual(frozenUiErrors(base, head), [], entry);
  }
});

test('exported names follow export * cycles and ignore external modules', () => {
  const tree = new Map([
    ['a/index.ts', "export * from './b.js';\nexport * from 'react';\nexport const A = 1;\n"],
    ['a/b.ts', "export * from './index.js';\nexport const B = 1;\n"],
  ]);
  assert.deepEqual(exportedNames(tree, 'a/index.ts').sort(), ['A', 'B']);
});

// The list.

test('a listed package that was deleted is reported, so the list only shrinks', () => {
  const head = baseTree();
  for (const path of [...head.keys()]) if (path.startsWith(`${CAMPAIGN}/`)) head.delete(path);
  const errors = frozenUiErrors(baseTree(), head);
  assert.equal(errors.length, 1);
  assert.match(errors[0], /FROZEN_UI_PACKAGES: lists features\/campaign, which no longer exists/);
});

test('a package outside the list may add UI freely', () => {
  const head = baseTree();
  head.set('features/run-page/src/renderer/panel.tsx', 'export const P = () => <button />;\n');
  head.set('features/run-page/package.json', '{}');
  assert.deepEqual(frozenUiErrors(baseTree(), head), []);
});

// The command line, driven with two directories and no Git.

async function writeTree(root, tree) {
  for (const [path, text] of tree) {
    await mkdir(dirname(join(root, path)), {recursive: true});
    await writeFile(join(root, path), text);
  }
}

test('the command line exits 1 naming the violation, and 0 when there is none', async t => {
  const directory = await mkdtemp(join(tmpdir(), 'vibesys-frozen-ui-'));
  t.after(() => rm(directory, {recursive: true, force: true}));
  const [base, same, grown] = ['base', 'same', 'grown'].map(name => join(directory, name));
  const head = baseTree();
  head.set(`${RENDERER}/fresh.tsx`, CLEAN_VIEW);
  await writeTree(base, baseTree());
  await writeTree(same, baseTree());
  await writeTree(grown, head);
  const run = (...args) => spawnSync('node', [SCRIPT, ...args], {encoding: 'utf8'});
  const refused = run('--base-dir', base, '--head-dir', grown);
  assert.equal(refused.status, 1);
  assert.match(refused.stderr, new RegExp(`frozen-ui: ${RENDERER}/fresh\\.tsx: a new file`));
  assert.equal(run('--base-dir', base, '--head-dir', same).status, 0);
  assert.equal(run().status, 2);
});
