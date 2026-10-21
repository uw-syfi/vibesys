import assert from 'node:assert/strict';
import {mkdir, mkdtemp, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname, join} from 'node:path';
import test from 'node:test';
import {
  domainWords,
  LAYER_FOLDERS,
  owningEntry,
  packageGraphErrors,
  RULES,
  STRICT_LINT_PLUGINS,
  workspaceSpecifiers,
} from './check_package_graph.mjs';

// Each fixture is a whole workspace that obeys every rule, plus one mutation. Building the
// violation from the valid graph (rather than a hand-made minimal tree) proves the rule fires for
// exactly the change it is about: the valid workspace itself reports nothing.

const BASE = {
  compilerOptions: {
    target: 'ES2022',
    lib: ['ES2022'],
    types: [],
    module: 'NodeNext',
    moduleResolution: 'NodeNext',
    composite: true,
  },
};

/** A tsconfig project; `depth` is how many folders below the workspace root its package sits. */
function project({lib, types, include, references = [], depth = 2}) {
  return {
    extends: `${'../'.repeat(depth)}tools/tsconfig.base.json`,
    compilerOptions: {...(lib ? {lib} : {}), ...(types ? {types} : {})},
    include,
    references: references.map(path => ({path})),
  };
}

function source(entry) {
  return {
    '@vibesys/source': `./src/${entry}.ts`,
    types: `./dist/${entry}.d.ts`,
    default: `./dist/${entry}.js`,
  };
}

/** An Electron app: a `node` main process and a sandboxed preload, its own runtime. */
function desktopMainPackage() {
  return {
    manifest: {
      name: '@vibesys/app-desktop-main',
      vibesys: {layer: 'app', entries: {'.': 'node', './preload-run': 'preload'}},
      exports: {
        '.': source('main/index'),
        './preload-run': {
          '@vibesys/source': './src/preload/run.cts',
          default: './dist/preload/run.cjs',
        },
      },
      dependencies: {'@vibesys/connection': 'workspace:*', '@vibesys/contracts': 'workspace:*'},
    },
    files: {
      'src/main/index.ts':
        "import 'electron';\nimport 'node:path';\nexport type {E} from '@vibesys/connection';\n",
      // A sandboxed preload loads only Electron; the workspace types it names are erased.
      'src/preload/run.cts':
        "import electron = require('electron');\nimport type {RunEvent} from '@vibesys/contracts';\n" +
        "type Contracts = typeof import('@vibesys/contracts');\n" +
        'export type P = [RunEvent, Contracts, typeof electron];\n',
      'tsconfig.lib.json': project({
        types: ['node'],
        include: ['src/main'],
        references: ['../../libs/connection/tsconfig.lib.json'],
      }),
      'tsconfig.preload-run.json': project({
        lib: ['ES2022', 'DOM'],
        types: ['node'],
        include: ['src/preload/run.cts'],
        references: ['../../libs/contracts/tsconfig.lib.json'],
      }),
    },
  };
}

/** The design system's package: components, the token table and the generated stylesheet. */
function uiPackage() {
  return {
    manifest: {
      name: '@vibesys/ui',
      vibesys: {
        layer: 'foundation',
        entries: {'.': 'dom', './tokens': 'neutral', './tokens.css': 'dom'},
      },
      exports: {
        '.': source('index'),
        './tokens': source('tokens/index'),
        './tokens.css': {'@vibesys/source': './src/tokens.css', default: './src/tokens.css'},
      },
      dependencies: {react: '19.1.1'},
    },
    files: {
      'src/index.ts': "import 'react';\nexport const button = 1;\n",
      'src/index.test.ts': "test('a button for any run', () => {});\n",
      'src/tokens/index.ts': 'export const space = 4;\n',
      'src/tokens.css': ':root { --accent: blue; }\n',
      'tsconfig.lib.json': project({lib: ['ES2022', 'DOM'], include: ['src']}),
      'tsconfig.tokens.json': project({include: ['src/tokens']}),
    },
  };
}

/** A strict feature: UI from `@vibesys/ui` only, a machine, no stylesheet. */
function strictScreenPackage() {
  return {
    manifest: {
      name: '@vibesys/feature-screen',
      vibesys: {
        layer: 'feature',
        entries: {'./state': 'neutral', './renderer': 'dom'},
      },
      exports: {'./state': source('state/index'), './renderer': source('renderer/index')},
      dependencies: {
        '@vibesys/contracts': 'workspace:*',
        '@vibesys/ui': 'workspace:*',
        react: '19.1.1',
        xstate: '5.33.2',
      },
    },
    files: {
      'src/state/index.ts':
        "import {setup} from 'xstate';\nimport type {RunEvent} from '@vibesys/contracts';\n" +
        'export const machine = setup({});\nexport type S = RunEvent;\n',
      'src/renderer/index.ts':
        "import 'react/jsx-runtime';\nimport {button} from '@vibesys/ui';\n" +
        "import {space} from '@vibesys/ui/tokens';\nexport const r = [button, space];\n",
      // Tests are not UI: a strict package's tests may use any test library.
      'src/renderer/index.test.ts': "import '@radix-ui/react-dialog';\nimport './index.js';\n",
      'tsconfig.state.json': project({
        include: ['src/state'],
        references: ['../../libs/contracts/tsconfig.lib.json'],
      }),
      'tsconfig.renderer.json': project({
        lib: ['ES2022', 'DOM'],
        include: ['src/renderer'],
        references: ['./tsconfig.state.json', '../../libs/ui/tsconfig.lib.json'],
      }),
    },
  };
}

/** The valid workspace: one package per layer shape the design uses. */
function validWorkspace() {
  return {
    contracts: {
      manifest: {
        name: '@vibesys/contracts',
        vibesys: {layer: 'foundation', entries: {'.': 'neutral'}},
        exports: {'.': source('index')},
      },
      files: {
        'src/index.ts': 'export type RunEvent = {type: string};\n',
        'tsconfig.lib.json': project({include: ['src']}),
      },
    },
    ui: uiPackage(),
    connection: {
      manifest: {
        name: '@vibesys/connection',
        vibesys: {layer: 'service', entries: {'.': 'node'}},
        exports: {'.': source('index')},
        dependencies: {'@vibesys/contracts': 'workspace:*'},
      },
      files: {
        'src/index.ts':
          "import 'node:net';\nimport type {RunEvent} from '@vibesys/contracts';\nexport type E = RunEvent;\n",
        'tsconfig.lib.json': project({
          types: ['node'],
          include: ['src'],
          references: ['../contracts/tsconfig.lib.json'],
        }),
      },
    },
    'stop-run': {
      manifest: {
        name: '@vibesys/feature-stop-run',
        vibesys: {
          layer: 'feature',
          entries: {'./state': 'neutral', './renderer': 'dom', './main': 'node'},
        },
        exports: {
          './state': source('state/index'),
          './renderer': source('renderer/index'),
          './main': source('main/index'),
        },
        dependencies: {
          '@vibesys/connection': 'workspace:*',
          '@vibesys/contracts': 'workspace:*',
          '@vibesys/ui': 'workspace:*',
        },
      },
      files: {
        'src/state/index.ts':
          "import type {RunEvent} from '@vibesys/contracts';\nexport type S = RunEvent;\n",
        'src/renderer/index.ts': "import {button} from '@vibesys/ui';\nexport const r = button;\n",
        'src/main/index.ts': "import 'electron';\nexport type {E} from '@vibesys/connection';\n",
        'src/main/index.test.ts': "import '@vibesys/connection';\n",
        'tsconfig.state.json': project({
          include: ['src/state'],
          references: ['../../libs/contracts/tsconfig.lib.json'],
        }),
        'tsconfig.renderer.json': project({
          lib: ['ES2022', 'DOM'],
          include: ['src/renderer'],
          references: ['./tsconfig.state.json', '../../libs/ui/tsconfig.lib.json'],
        }),
        'tsconfig.main.json': project({
          types: ['node'],
          include: ['src/main'],
          references: ['./tsconfig.state.json', '../../libs/connection/tsconfig.lib.json'],
        }),
      },
    },
    screen: strictScreenPackage(),
    'desktop-main': desktopMainPackage(),
    'web-app': {
      manifest: {
        name: '@vibesys/app-web',
        vibesys: {layer: 'app', entries: {'.': 'dom'}},
        dependencies: {'@vibesys/feature-stop-run': 'workspace:*'},
      },
      files: {
        'src/index.ts':
          "import {r} from '@vibesys/feature-stop-run/renderer';\nexport const a = r;\n",
        'tsconfig.lib.json': project({
          lib: ['ES2022', 'DOM'],
          include: ['src'],
          references: ['../../features/stop-run/tsconfig.renderer.json'],
        }),
      },
    },
  };
}

/** Where a fixture package is filed: its layer's folder, unless the fixture says otherwise. */
function directoryOf(label, {manifest, directory}) {
  return directory ?? `${LAYER_FOLDERS[manifest.vibesys?.layer] ?? 'libs'}/${label}`;
}

async function materialize(t, workspace) {
  const root = await mkdtemp(join(tmpdir(), 'vibesys-package-graph-'));
  t.after(() => rm(root, {recursive: true, force: true}));
  const directories = Object.entries(workspace).map(([label, entry]) => [
    directoryOf(label, entry),
    entry,
  ]);
  // The layer folders are groups; a package filed anywhere else is a literal workspace member.
  const literals = directories.map(([directory]) => directory).filter(path => !path.includes('/'));
  const patterns = [...literals, 'libs/*', 'features/*', 'apps/*'];
  await writeFile(
    join(root, 'pnpm-workspace.yaml'),
    `packages:\n${patterns.map(pattern => `  - "${pattern}"\n`).join('')}`,
  );
  const includes = [
    'features/**/src/**/*.ts',
    'features/**/src/**/*.tsx',
    'apps/**/src/**/*.ts',
    'apps/**/src/**/*.tsx',
    '!**/*.test.ts',
    '!**/*.test.tsx',
  ];
  await writeFile(
    join(root, 'biome.json'),
    JSON.stringify({overrides: [{includes, plugins: STRICT_LINT_PLUGINS}]}),
  );
  await mkdir(join(root, 'tools'), {recursive: true});
  await writeFile(join(root, 'tools', 'tsconfig.base.json'), JSON.stringify(BASE));
  for (const [directory, {manifest, files}] of directories) {
    await mkdir(join(root, directory), {recursive: true});
    await writeFile(join(root, directory, 'package.json'), JSON.stringify(manifest));
    for (const [path, content] of Object.entries(files)) {
      await mkdir(dirname(join(root, directory, path)), {recursive: true});
      const text = typeof content === 'string' ? content : JSON.stringify(content);
      await writeFile(join(root, directory, path), text);
    }
  }
  return root;
}

/** One mutation of the valid workspace per rule; each must break that rule and only that rule. */
const RULE_CASES = {
  declarations: workspace => {
    workspace.ui.manifest.vibesys = {layer: 'widgets', entries: {'.': 'dom'}};
  },
  'entries-match-exports': workspace => {
    workspace.contracts.manifest.vibesys.entries['./testing'] = 'neutral';
  },
  'exports-name-source': workspace => {
    workspace.contracts.manifest.exports['.'] = {
      types: './dist/index.d.ts',
      default: './dist/index.js',
    };
  },
  'feature-entries': workspace => {
    workspace['stop-run'].manifest.vibesys.entries['./main'] = 'dom';
    workspace['stop-run'].files['tsconfig.main.json'].compilerOptions = {lib: ['ES2022', 'DOM']};
  },
  'layer-direction': workspace => {
    // A feature depending on another feature: peers share a layer, so this is a layer violation.
    workspace.other = structuredClone(workspace['stop-run']);
    workspace.other.manifest.name = '@vibesys/feature-other';
    workspace.other.manifest.dependencies['@vibesys/feature-stop-run'] = 'workspace:*';
    workspace.other.files['tsconfig.state.json'].references.push({
      path: '../stop-run/tsconfig.state.json',
    });
  },
  'workspace-protocol': workspace => {
    workspace.connection.manifest.dependencies['@vibesys/contracts'] = '^0.1.0';
  },
  'imports-are-workspace-packages': workspace => {
    workspace.contracts.files['src/index.ts'] += "import '@vibesys/nowhere';\n";
  },
  'imports-are-declared': workspace => {
    workspace['web-app'].files['src/index.ts'] += "import '@vibesys/ui';\n";
  },
  'imports-use-exports': workspace => {
    workspace['web-app'].files['src/index.ts'] += "import '@vibesys/feature-stop-run/src/main';\n";
  },
  'preload-sandbox': workspace => {
    // A value import: the sandbox's require cannot load a workspace package.
    workspace['desktop-main'].files['src/preload/run.cts'] =
      "import {RunEvent} from '@vibesys/contracts';\nexport type P = RunEvent;\n";
  },
  'external-dependencies': workspace => {
    // The pure state entry reaching a Node builtin.
    workspace['stop-run'].files['src/state/index.ts'] += "import 'node:fs';\n";
  },
  'runtime-reachability': workspace => {
    // A renderer entry reaching a Node-only package.
    workspace['stop-run'].files['src/renderer/index.ts'] += "import '@vibesys/connection';\n";
  },
  'entry-projects': workspace => {
    delete workspace.ui.files['tsconfig.lib.json'];
  },
  'entry-capability': workspace => {
    // A state entry compiled with Node types would let a Node import type-check.
    workspace['stop-run'].files['tsconfig.state.json'].compilerOptions = {types: ['node']};
  },
  'references-match-dependencies': workspace => {
    workspace.connection.files['tsconfig.lib.json'].references = [];
  },
  'domain-free': workspace => {
    workspace.ui.files['src/index.ts'] += 'export const runStatus = 2;\n';
    workspace.ui.files['src/tokens.css'] += '.host-row { color: red; }\n';
  },
  'bucket-names': workspace => {
    // A foundation package named for where things go rather than what it owns.
    workspace['shared-models'] = structuredClone(workspace.contracts);
    workspace['shared-models'].manifest.name = '@vibesys/shared-models';
  },
  'layer-matches-folder': workspace => {
    workspace.misfiled = misfiledLibrary('apps/misfiled');
  },
  'strict-no-stylesheets': workspace => {
    workspace.screen.files['src/renderer/screen.css'] = '.screen { color: red; }\n';
  },
  'strict-imports': workspace => {
    workspace.screen.files['src/renderer/index.ts'] += "import '@radix-ui/react-dialog';\n";
  },
};

/** A leaf foundation library filed at `directory`, which nothing depends on. */
function misfiledLibrary(directory) {
  const library = structuredClone(validWorkspace().contracts);
  library.manifest.name = '@vibesys/misfiled';
  library.directory = directory;
  library.files['tsconfig.lib.json'] = project({
    include: ['src'],
    depth: directory.split('/').length,
  });
  return library;
}

test('every package-graph rule has a failing fixture', () => {
  assert.deepEqual(Object.keys(RULE_CASES).sort(), [...RULES].sort());
});

test('the valid workspace has no violations', async t => {
  const root = await materialize(t, validWorkspace());
  assert.deepEqual(packageGraphErrors(root), []);
});

for (const [rule, mutate] of Object.entries(RULE_CASES)) {
  test(`${rule} rejects its fixture and nothing else fires`, async t => {
    const workspace = validWorkspace();
    mutate(workspace);
    const errors = packageGraphErrors(await materialize(t, workspace));
    assert.notEqual(errors.length, 0, `${rule} reported nothing`);
    for (const error of errors) assert.ok(error.startsWith(`${rule}: `), error);
  });
}

test('a preload reaches neither a Node entry nor a Node builtin', async t => {
  const workspace = validWorkspace();
  workspace['desktop-main'].files['src/preload/run.cts'] =
    "import type {E} from '@vibesys/connection';\nimport fs = require('node:fs');\n" +
    'export type P = [E, typeof fs];\n';
  const errors = packageGraphErrors(await materialize(t, workspace));
  assert.deepEqual(errors.map(error => error.split(':')[0]).sort(), [
    'external-dependencies',
    'preload-sandbox',
    'runtime-reachability',
  ]);
});

test('test code may import a dev dependency of any runtime', async t => {
  const workspace = validWorkspace();
  const app = workspace['web-app'];
  app.manifest.devDependencies = {'@vibesys/connection': 'workspace:*'};
  app.files['tsconfig.lib.json'].references.push({
    path: '../../libs/connection/tsconfig.lib.json',
  });
  app.files['src/index.test.ts'] = "import 'node:fs';\nimport '@vibesys/connection';\n";
  assert.deepEqual(packageGraphErrors(await materialize(t, workspace)), []);
});

const EXTERNAL_CASES = {
  'React in a pure state entry': workspace => {
    workspace['stop-run'].files['src/state/index.ts'] += "import 'react/jsx-runtime';\n";
  },
  'Electron in a renderer entry': workspace => {
    workspace['stop-run'].files['src/renderer/index.ts'] += "import 'electron';\n";
  },
  'Electron in a service package': workspace => {
    workspace.connection.files['src/index.ts'] += "import 'electron';\n";
  },
  'a bare Node builtin in a dom entry': workspace => {
    workspace.ui.files['src/index.ts'] += "import 'fs';\n";
  },
  'a Node builtin in a neutral entry': workspace => {
    workspace.contracts.files['src/index.ts'] += "import 'node:fs';\n";
  },
  'React declared by a package with no dom entry': workspace => {
    workspace.contracts.manifest.dependencies = {'react-dom': '19.1.1'};
  },
  // Control state is a machine; a fold, a model, a primitive, or a wire package is not.
  'XState in a foundation package': workspace => {
    workspace.contracts.files['src/index.ts'] += "import 'xstate';\n";
  },
  'XState in an app': workspace => {
    workspace['web-app'].files['src/index.ts'] += "import {setup} from 'xstate';\n";
  },
  "XState in a feature's main-process entry": workspace => {
    workspace['stop-run'].files['src/main/index.ts'] += "import 'xstate';\n";
  },
  'XState declared by a package that may not use it': workspace => {
    workspace.ui.manifest.dependencies.xstate = '5.33.2';
  },
};

test('XState is allowed in a feature state and renderer entry and in a machine package', async t => {
  const workspace = validWorkspace();
  workspace['stop-run'].manifest.dependencies.xstate = '5.33.2';
  workspace['stop-run'].files['src/state/index.ts'] += "import {setup} from 'xstate';\n";
  workspace['stop-run'].files['src/renderer/index.ts'] += "import {createActor} from 'xstate';\n";
  workspace.machine = {
    manifest: {
      name: '@vibesys/machine',
      vibesys: {layer: 'core', entries: {'.': 'neutral'}},
      exports: {'.': source('index')},
      dependencies: {xstate: '5.33.2'},
    },
    files: {
      'src/index.ts': "export {setup} from 'xstate';\n",
      'tsconfig.lib.json': project({include: ['src']}),
    },
  };
  // The connection manager is a machine package: its host connection is a machine.
  workspace.connection.manifest.dependencies.xstate = '5.33.2';
  workspace.connection.files['src/index.ts'] += "import {setup} from 'xstate';\n";
  assert.deepEqual(packageGraphErrors(await materialize(t, workspace)), []);
});

for (const [name, mutate] of Object.entries(EXTERNAL_CASES)) {
  test(`external-dependencies rejects ${name}`, async t => {
    const workspace = validWorkspace();
    mutate(workspace);
    const errors = packageGraphErrors(await materialize(t, workspace));
    assert.equal(errors.length, 1, errors.join('\n'));
    assert.ok(errors[0].startsWith('external-dependencies: '), errors[0]);
  });
}

test('specifiers in comments and strings are not imports', () => {
  const text =
    "// import '@vibesys/ui';\nconst s = \"from '@vibesys/ui'\";\n" +
    "import a from '@vibesys/contracts';\nexport * from '@vibesys/x/y';\nawait import('@vibesys/z');\n";
  assert.deepEqual(workspaceSpecifiers(text), ['@vibesys/contracts', '@vibesys/x/y', '@vibesys/z']);
});

test('a file belongs to its exact entry, else the deepest index directory', () => {
  const entries = [
    {subpath: '.', runtime: 'neutral', source: 'src/index.ts'},
    {subpath: './node', runtime: 'node', source: 'src/node/index.ts'},
    {subpath: './websocket', runtime: 'dom', source: 'src/websocket.ts'},
  ];
  assert.equal(owningEntry(entries, 'src/node/client.ts').subpath, './node');
  assert.equal(owningEntry(entries, 'src/websocket.ts').subpath, './websocket');
  assert.equal(owningEntry(entries, 'src/framer.ts').subpath, '.');
});

test('domain words are matched per word, across case changes and plurals', () => {
  assert.deepEqual(domainWords('const runStatus = hostKeys; // starting tasks'), [
    'run',
    'host',
    'task',
  ]);
  assert.deepEqual(domainWords('const trunk = ghost; brunch; Dialog; tokens'), []);
  assert.deepEqual(domainWords('running hosting; gatewayUrl replays transcripts listing'), [
    'run',
    'host',
    'gateway',
    'replay',
    'transcript',
    'listing',
  ]);
  assert.deepEqual(domainWords('a rounded string; padding, rendering'), []);
});

test('a bucket name is allowed above the domain layer and as part of a longer word', async t => {
  const workspace = validWorkspace();
  workspace['web-app'].manifest.name = '@vibesys/app-shared';
  workspace['web-app'].manifest.vibesys.layer = 'app';
  workspace['core-model'] = structuredClone(workspace.contracts);
  workspace['core-model'].manifest.name = '@vibesys/coresident';
  assert.deepEqual(packageGraphErrors(await materialize(t, workspace)), []);
});

test('a feature stylesheet is a dom asset but violates the design rule', async t => {
  const workspace = validWorkspace();
  const feature = workspace['stop-run'];
  feature.manifest.vibesys.entries['./styles.css'] = 'dom';
  feature.manifest.exports['./styles.css'] = {
    '@vibesys/source': './src/renderer/styles.css',
    default: './src/renderer/styles.css',
  };
  feature.files['src/renderer/styles.css'] = '.stop-button { color: red; }\n';
  assert.deepEqual(
    packageGraphErrors(await materialize(t, workspace)).map(error => error.split(':')[0]),
    ['strict-no-stylesheets', 'strict-no-stylesheets'],
  );
  feature.manifest.vibesys.entries['./styles.css'] = 'neutral';
  const errors = packageGraphErrors(await materialize(t, workspace));
  assert.deepEqual(
    errors.map(error => error.split(':')[0]),
    ['feature-entries'],
  );
});

test('a stylesheet entry is a dom asset with no project', async t => {
  const workspace = validWorkspace();
  workspace.ui.manifest.vibesys.entries['./tokens.css'] = 'neutral';
  const errors = packageGraphErrors(await materialize(t, workspace));
  assert.deepEqual(
    errors.map(error => error.split(':')[0]),
    ['entry-capability'],
  );
});

// layer-matches-folder: the manifest's layer is the truth, and the folder must mirror it.

function declareFeature({manifest, files}) {
  files['tsconfig.state.json'] = files['tsconfig.lib.json'];
  delete files['tsconfig.lib.json'];
  manifest.vibesys = {
    layer: 'feature',
    entries: {'./state': 'neutral'},
  };
  manifest.exports = {'./state': source('index')};
}

const FOLDER_CASES = [
  ['a library declaring feature', 'libs/misfiled', declareFeature],
  ['a library declaring app', 'libs/misfiled', ({manifest}) => (manifest.vibesys.layer = 'app')],
  ['a feature declaring core', 'features/misfiled', () => {}],
  [
    'an app declaring service',
    'apps/misfiled',
    ({manifest}) => (manifest.vibesys.layer = 'service'),
  ],
  ['a package in no layer folder', 'misfiled', () => {}],
];

for (const [name, directory, declare] of FOLDER_CASES) {
  test(`layer-matches-folder rejects ${name}`, async t => {
    const workspace = validWorkspace();
    workspace.misfiled = misfiledLibrary(directory);
    declare(workspace.misfiled);
    const errors = packageGraphErrors(await materialize(t, workspace));
    assert.equal(errors.length, 1, errors.join('\n'));
    assert.ok(errors[0].startsWith('layer-matches-folder: '), errors[0]);
    assert.ok(errors[0].includes(`${directory}/package.json`), errors[0]);
  });
}

test('layer-matches-folder accepts each library layer in libs ', async t => {
  const workspace = validWorkspace();
  // Named for a concept each, so the bucket-names rule has nothing to say.
  const libraries = {foundation: 'alpha', core: 'beta', domain: 'gamma', service: 'delta'};
  for (const [layer, name] of Object.entries(libraries)) {
    const library = misfiledLibrary(`libs/${name}`);
    library.manifest.name = `@vibesys/${name}`;
    library.manifest.vibesys.layer = layer;
    workspace[name] = library;
  }
  const errors = packageGraphErrors(await materialize(t, workspace));
  assert.deepEqual(errors, []);
});

// Design rules S2 and S6 apply to every feature and app. Each case changes one valid workspace
// and must be reported by the named rule with its location and fix.

const strictRules = errors => errors.filter(error => error.startsWith('strict-'));

async function strictErrorsOf(t, mutate) {
  const workspace = validWorkspace();
  mutate(workspace);
  return strictRules(packageGraphErrors(await materialize(t, workspace)));
}

test('declarations rejects the obsolete design marker', async t => {
  const workspace = validWorkspace();
  workspace.screen.manifest.vibesys.design = 'strict';
  const errors = packageGraphErrors(await materialize(t, workspace));
  assert.deepEqual(errors, [
    'declarations: features/screen/package.json: unknown key vibesys.design',
  ]);
});

test('declarations still rejects an unknown vibesys key', async t => {
  const workspace = validWorkspace();
  workspace.screen.manifest.vibesys.theme = 'strict';
  const errors = packageGraphErrors(await materialize(t, workspace));
  assert.deepEqual(errors, [
    'declarations: features/screen/package.json: unknown key vibesys.theme',
  ]);
});

const STYLESHEET_CASES = {
  'a CSS file': workspace => {
    workspace.screen.files['src/renderer/screen.css'] = '.a { color: red; }\n';
  },
  'a CSS module': workspace => {
    workspace.screen.files['src/renderer/screen.module.css'] = '.a { color: red; }\n';
  },
  'a Sass file': workspace => {
    workspace.screen.files['src/renderer/nested/screen.scss'] = '.a { .b { color: red; } }\n';
  },
  'a stylesheet outside src': workspace => {
    workspace.screen.files['styles/theme.less'] = '@a: red;\n';
  },
  'a stylesheet export': workspace => {
    workspace.screen.manifest.vibesys.entries['./styles.css'] = 'dom';
    workspace.screen.manifest.exports['./styles.css'] = {
      '@vibesys/source': './src/renderer/styles.css',
      default: './src/renderer/styles.css',
    };
    workspace.screen.files['src/renderer/styles.css'] = '.a { color: red; }\n';
  },
};

for (const [name, mutate] of Object.entries(STYLESHEET_CASES)) {
  test(`strict-no-stylesheets rejects ${name}, naming the file and the fix`, async t => {
    const errors = await strictErrorsOf(t, mutate);
    assert.notEqual(errors.length, 0);
    for (const error of errors) {
      assert.match(
        error,
        /^strict-no-stylesheets: features\/screen\/\S+: .*\(S2\).*@vibesys\/ui/,
        error,
      );
    }
  });
}

test('an unmarked feature still cannot ship a stylesheet', async t => {
  const workspace = validWorkspace();
  workspace.screen.files['src/renderer/screen.css'] = '.a { color: red; }\n';
  const errors = packageGraphErrors(await materialize(t, workspace));
  assert.ok(errors.some(error => error.startsWith('strict-no-stylesheets: features/screen/')));
});

test('an unmarked app cannot import a component kit', async t => {
  const workspace = validWorkspace();
  workspace['web-app'].files['src/index.ts'] += "import 'lucide-react';\n";
  const errors = packageGraphErrors(await materialize(t, workspace));
  assert.ok(errors.some(error => error.startsWith('strict-imports: apps/web-app/src/index.ts:')));
});

const RENDERER = 'features/screen/src/renderer/index.ts';

/** A strict import of `specifier` appended to a file: it lands on line 5. */
function importing(file, specifier) {
  return {
    mutate: workspace => {
      const path = file.replace('features/screen/', '');
      workspace.screen.files[path] += `import '${specifier}';\n`;
    },
    where: `${file}:5`,
    names: specifier,
  };
}

const IMPORT_CASES = {
  'a component kit': importing(RENDERER, '@radix-ui/react-dialog'),
  'a utility class helper': importing(RENDERER, 'class-variance-authority'),
  'CSS-in-JS': importing(RENDERER, 'styled-components'),
  'an icon package': importing(RENDERER, 'lucide-react'),
  'the stylesheet entry of @vibesys/ui': importing(RENDERER, '@vibesys/ui/tokens.css'),
};

for (const [name, {mutate, where, names}] of Object.entries(IMPORT_CASES)) {
  test(`strict-imports rejects ${name}, naming the file, line and fix`, async t => {
    const errors = await strictErrorsOf(t, mutate);
    const found = errors.filter(error => error.startsWith(`strict-imports: ${where}: `));
    assert.equal(found.length, 1, errors.join('\n'));
    assert.ok(found[0].includes(names), found[0]);
    assert.match(found[0], /\(S6\)/);
    assert.match(found[0], /@vibesys\/ui/);
  });
}

test('strict-imports rejects a UI kit declared in the manifest, naming the key', async t => {
  const errors = await strictErrorsOf(t, workspace => {
    workspace.screen.manifest.dependencies['lucide-react'] = '1.0.0';
  });
  assert.equal(errors.length, 1, errors.join('\n'));
  assert.match(
    errors[0],
    /^strict-imports: features\/screen\/package\.json: dependencies\["lucide-react"\] \(S6\)/,
  );
});

test('strict-imports allows the listed libraries, react-dom and tests', async t => {
  const workspace = validWorkspace();
  workspace['run-skeleton'] = {
    manifest: {
      name: '@vibesys/run-skeleton',
      vibesys: {layer: 'domain', entries: {'.': 'neutral'}},
      exports: {'.': source('index')},
    },
    files: {
      'src/index.ts': 'export const skeleton = 1;\n',
      'tsconfig.lib.json': project({include: ['src']}),
    },
  };
  workspace.screen.manifest.dependencies['@vibesys/run-skeleton'] = 'workspace:*';
  workspace.screen.files['src/renderer/index.ts'] += "import '@vibesys/run-skeleton';\n";
  workspace.screen.files['tsconfig.renderer.json'].references.push({
    path: '../../libs/run-skeleton/tsconfig.lib.json',
  });
  workspace.screen.files['src/renderer/index.ts'] +=
    "import 'react-dom/client';\nimport '@vibesys/connection';\nimport '@vibesys/ui';\n";
  workspace.screen.manifest.dependencies['@vibesys/connection'] = 'workspace:*';
  workspace.screen.manifest.dependencies['react-dom'] = '19.1.1';
  workspace.screen.files['tsconfig.renderer.json'].references.push({
    path: '../../libs/connection/tsconfig.lib.json',
  });
  const errors = packageGraphErrors(await materialize(t, workspace));
  assert.deepEqual(strictRules(errors), []);
});

test('a strict app imports the generated stylesheets of @vibesys/ui', async t => {
  const workspace = validWorkspace();
  const app = workspace['web-app'];
  app.manifest.dependencies = {'@vibesys/ui': 'workspace:*'};
  app.files['tsconfig.lib.json'].references = [{path: '../../libs/ui/tsconfig.lib.json'}];
  app.files['src/index.ts'] =
    "import {button} from '@vibesys/ui';\nimport '@vibesys/ui/tokens.css';\nexport const a = button;\n";
  app.files['src/index.test.ts'] = "import '@radix-ui/react-dialog';\n";
  assert.deepEqual(strictRules(packageGraphErrors(await materialize(t, workspace))), []);
});

test('an app composes feature screens without marker declarations', async t => {
  const workspace = validWorkspace();
  const app = workspace['web-app'];
  app.manifest.dependencies = {'@vibesys/feature-screen': 'workspace:*'};
  app.files['tsconfig.lib.json'].references = [
    {path: '../../features/screen/tsconfig.renderer.json'},
  ];
  app.files['src/index.ts'] =
    "import {r} from '@vibesys/feature-screen/renderer';\nexport const a = r;\n";
  assert.deepEqual(strictRules(packageGraphErrors(await materialize(t, workspace))), []);

  app.manifest.dependencies['@vibesys/feature-stop-run'] = 'workspace:*';
  app.files['src/index.ts'] += "import '@vibesys/feature-stop-run/renderer';\n";
  assert.deepEqual(strictRules(packageGraphErrors(await materialize(t, workspace))), []);
});
