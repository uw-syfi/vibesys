import assert from 'node:assert/strict';
import {mkdir, mkdtemp, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname, join} from 'node:path';
import test from 'node:test';
import {
  domainWords,
  owningEntry,
  packageGraphErrors,
  RULES,
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

function project({lib, types, include, references = []}) {
  return {
    extends: '../tsconfig.base.json',
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
    ui: {
      manifest: {
        name: '@vibesys/ui',
        vibesys: {layer: 'foundation', entries: {'.': 'dom', './tokens.css': 'dom'}},
        exports: {
          '.': source('index'),
          './tokens.css': {'@vibesys/source': './src/tokens.css', default: './src/tokens.css'},
        },
        dependencies: {react: '19.1.1'},
      },
      files: {
        'src/index.ts': "import 'react';\nexport const button = 1;\n",
        'src/index.test.ts': "test('a button for any run', () => {});\n",
        'src/tokens.css': ':root { --accent: blue; }\n',
        'tsconfig.lib.json': project({lib: ['ES2022', 'DOM'], include: ['src']}),
      },
    },
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
          references: ['../contracts/tsconfig.lib.json'],
        }),
        'tsconfig.renderer.json': project({
          lib: ['ES2022', 'DOM'],
          include: ['src/renderer'],
          references: ['./tsconfig.state.json', '../ui/tsconfig.lib.json'],
        }),
        'tsconfig.main.json': project({
          types: ['node'],
          include: ['src/main'],
          references: ['./tsconfig.state.json', '../connection/tsconfig.lib.json'],
        }),
      },
    },
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
          references: ['../stop-run/tsconfig.renderer.json'],
        }),
      },
    },
  };
}

async function materialize(t, workspace) {
  const root = await mkdtemp(join(tmpdir(), 'vibesys-package-graph-'));
  t.after(() => rm(root, {recursive: true, force: true}));
  await writeFile(join(root, 'pnpm-workspace.yaml'), 'packages:\n  - "*"\n');
  await writeFile(join(root, 'tsconfig.base.json'), JSON.stringify(BASE));
  for (const [directory, {manifest, files}] of Object.entries(workspace)) {
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
};

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

test('legacy packages skip only the per-entry project rules', async t => {
  const workspace = validWorkspace();
  workspace.legacy = {
    manifest: {
      name: '@vibesys/desktop',
      vibesys: {layer: 'app', entries: {'.': 'dom'}},
      dependencies: {'@vibesys/connection': 'workspace:*'},
    },
    files: {'src/index.ts': "import '@vibesys/connection';\n"},
  };
  const errors = packageGraphErrors(await materialize(t, workspace));
  assert.deepEqual(
    errors.map(error => error.split(':')[0]),
    ['runtime-reachability'],
  );
});

test('test code may import a dev dependency of any runtime', async t => {
  const workspace = validWorkspace();
  const app = workspace['web-app'];
  app.manifest.devDependencies = {'@vibesys/connection': 'workspace:*'};
  app.files['tsconfig.lib.json'].references.push({path: '../connection/tsconfig.lib.json'});
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
  'the terminal UI runtime outside an app': workspace => {
    workspace.contracts.files['src/index.ts'] += "import '@opentui/core';\n";
  },
  'React declared by a package with no dom entry': workspace => {
    workspace.contracts.manifest.dependencies = {'react-dom': '19.1.1'};
  },
};

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

test('a stylesheet entry is a dom asset with no project', async t => {
  const workspace = validWorkspace();
  workspace.ui.manifest.vibesys.entries['./tokens.css'] = 'neutral';
  const errors = packageGraphErrors(await materialize(t, workspace));
  assert.deepEqual(
    errors.map(error => error.split(':')[0]),
    ['entry-capability'],
  );
});
