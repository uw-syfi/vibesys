import {fileURLToPath} from 'node:url';
import {pathAlternation, workspaceLayout} from './tools/scripts/workspace_layout.mjs';

// Scanned sources: each package's `src/`, plus the non-shipping code that lives next to it (the
// benchmarks) and the workspace's own tooling (`tools`). Both sets come from the workspace layout
// rather than a name list, so a new
// package, or a new directory inside one, is covered by these rules on its first commit.
// `tools/scripts/check_ts_architecture.mjs` derives the scan roots from the same place.
const layout = workspaceLayout(fileURLToPath(new URL('.', import.meta.url)));
const PACKAGE_DIRECTORIES = pathAlternation(layout.packages.map(({directory}) => directory));
const PACKAGES = `^(?:${PACKAGE_DIRECTORIES})/`;
// Capturing, so a rule can exclude the importer's own package with a `$1` back-reference.
const OWN_PACKAGE = `^(${PACKAGE_DIRECTORIES})/`;
const TOOLING = `^(?:${pathAlternation(layout.toolingDirectories)})/`;
const SCANNED = `${PACKAGES}|${TOOLING}`;
const TEST_FILE = '\\.test\\.[cm]?[jt]sx?$';
const TRANSPORT_TEST_ONLY = '^libs/(?:transport|contracts)/src/(?:test-support/|testing/)';
const STORE_TEST_ONLY = '^libs/store/src/testing/';
// A package's own test-support directory (`connection/src/testing/`): Fakes that only tests import.
const PACKAGE_TEST_SUPPORT = '^libs/[^/]+/src/testing/';
// Any package's test support, at any depth of its sources: the `testing/` entries that ship Fakes
// to other packages' tests, and the `test-support/` helpers.
// Two patterns, because the one-pattern `src/(?:.+/)?` nests quantifiers that dependency-cruiser's
// regular-expression safety check rejects.
const ANY_TEST_SUPPORT = [
  `^(?:${PACKAGE_DIRECTORIES})/src/(?:testing|test-support)/`,
  `^(?:${PACKAGE_DIRECTORIES})/src/.*/(?:testing|test-support)/`,
];
const LEAF_TOOL_DIRECTORIES = pathAlternation(layout.toolingDirectories);
const LEAF_TOOLS = `^(?:${LEAF_TOOL_DIRECTORIES})/`;
// The importer of a `tools-are-leaves` edge: any file in a leaf tool directory, or any other file
// in a package. Group 1 is the importer's own leaf tool directory, so `to.pathNot: '^$1/'`
// excludes exactly that directory and no other tool's. Ordinary package code matches the second
// alternative, where group 1 does not participate, so `$1` is never substituted and the resulting
// `^$1/` matches no path: such an importer may reach no tool directory at all.
const LEAF_TOOL_IMPORTER = `^(${LEAF_TOOL_DIRECTORIES})/|${PACKAGES}`;

// A feature package (`features/<name>/src/`) is a pure `state/` core (its `./state` entry) and a
// `renderer/` (its `./renderer` entry) of presentational `ui/` and an `effects/` shell that calls
// the interfaces the app injects. Which packages a feature may reach is the package-graph
// checker's; these rules hold the layers inside one feature package.
// Only a feature's browser parts: a later `main/` (Node) entry is not held to the renderer rules.
const FEATURE = '^features/[^/]+/src/(?:state|renderer)/';
const RENDERER_PART = part => `^features/[^/]+/src/renderer/${part}/`;
const TRANSPORT = ['^libs/transport(?:-node)?/', '/node_modules/@vibesys/transport(?:-node)?/'];
// The protocol vocabulary, which presentational code never names.
const CONTRACTS = ['^libs/contracts/', '/node_modules/@vibesys/contracts/'];

function featureRules() {
  return [
    {
      // The functional core does no I/O: no React, no renderer, no transport runtime values.
      // Generated types and the run fold are the inputs it reads. Group 1 is the importer's own
      // feature, whose renderer its state never reaches. A core that needs a DOM global (the
      // gateway form parses with `URL`) sits in `renderer/state/` and is held to the same rule.
      name: 'feature-state-is-pure',
      severity: 'error',
      from: {path: '^features/([^/]+)/src/(?:renderer/)?state/', pathNot: TEST_FILE},
      to: {
        path: [
          '^features/$1/src/renderer/(?!state/)',
          '(?:^|/)node_modules/(?:react|react-dom|electron)/',
          ...TRANSPORT,
          '^libs/request-identity/',
        ],
        dependencyTypesNot: ['type-only'],
      },
    },
    {
      // Presentational components render props and report events; `ui-no-*.grit` forbids their
      // I/O calls and effect hooks, and this forbids reaching anything that does I/O.
      name: 'feature-ui-does-not-depend-on-shell',
      severity: 'error',
      from: {path: RENDERER_PART('ui'), pathNot: TEST_FILE},
      to: {
        path: [
          RENDERER_PART('effects'),
          ...TRANSPORT,
          ...CONTRACTS,
          '(?:^|/)node_modules/electron/',
        ],
      },
    },
    {
      name: 'feature-effects-do-not-import-ui',
      severity: 'error',
      from: {path: RENDERER_PART('effects'), pathNot: TEST_FILE},
      to: {path: RENDERER_PART('ui')},
    },
    {
      // A feature's effects call the interfaces its `./state` declares; the web app's backend
      // adapter is the one module that imports @vibesys/transport's runtime values.
      name: 'feature-transport-is-type-only',
      severity: 'error',
      from: {path: FEATURE, pathNot: TEST_FILE},
      to: {path: TRANSPORT, dependencyTypesNot: ['type-only']},
    },
  ];
}

// The desktop app's Electron main process is composed from packages, as its renderer is
// (`docs/contributing/clients-architecture.md`): `@vibesys/connection` (the connection manager, a
// pure `state/` machine and a `shell/` that owns its processes, channels, and timers),
// `@vibesys/settings`, the features' `./main` controllers, and `apps/desktop-main`, which composes
// them in `src/main/` and holds the sandboxed preloads in `src/preload/`. Which package may reach
// which is the package-graph checker's (a preload is its own runtime there); these rules hold the
// modules inside those packages.
const DESKTOP_MAIN = '^apps/desktop-main/src/main/';
const DESKTOP_PRELOAD = '^apps/desktop-main/src/preload/';
const CONNECTION_STATE = '^libs/connection/src/state/';
const CONNECTION_SHELL = '^libs/connection/src/shell/';
// Every module of the main process: the app, the packages only it runs, the features' `./main`.
const MAIN_PROCESS = [
  '^apps/desktop-main/src/',
  '^libs/connection/src/',
  '^libs/settings/src/',
  '^features/[^/]+/src/main/',
];
// The desktop shell app (`apps/desktop-shell/src`) is the one desktop renderer.
const DESKTOP_SHELL = '^apps/desktop-shell/src/';
const UI_RUNTIME = '(?:^|/)node_modules/(?:react|react-dom|electron)/';

function desktopRules() {
  return [
    {
      // A preload is the sandboxed page's only way to the main process, and runs before the page
      // with Electron's renderer APIs: it binds `@vibesys/ipc`'s typed messages to Electron and
      // nothing else. A feature or domain package would put product code in the trusted bridge;
      // the payload types it carries are reached through `@vibesys/ipc`'s bridge interfaces.
      name: 'desktop-preloads-import-only-ipc-and-electron',
      severity: 'error',
      from: {path: DESKTOP_PRELOAD, pathNot: TEST_FILE},
      to: {pathNot: ['^libs/ipc/src/', '(?:^|/)node_modules/electron/']},
    },
    {
      // The main process loads a preload by path into a page; it never runs one itself.
      name: 'desktop-main-does-not-import-preloads',
      severity: 'error',
      from: {path: DESKTOP_MAIN},
      to: {path: DESKTOP_PRELOAD},
    },
    {
      // The connection manager's functional core folds events into state and requests: no Node or
      // Electron, no shell, no view.
      name: 'connection-state-is-pure',
      severity: 'error',
      from: {path: CONNECTION_STATE, pathNot: TEST_FILE},
      to: {
        path: [CONNECTION_SHELL, UI_RUNTIME, ...TRANSPORT, ...CONTRACTS, '^libs/request-identity/'],
        dependencyTypesNot: ['type-only'],
      },
    },
    {
      name: 'connection-state-has-no-node-runtime',
      severity: 'error',
      from: {path: CONNECTION_STATE, pathNot: TEST_FILE},
      to: {dependencyTypes: ['core']},
    },
    {
      // Only the connection manager's shell starts processes or opens sockets in the main process.
      name: 'desktop-only-connection-shell-opens-processes-and-sockets',
      severity: 'error',
      from: {path: MAIN_PROCESS, pathNot: [TEST_FILE, CONNECTION_SHELL]},
      to: {dependencyTypes: ['core'], path: '^(?:node:)?(?:net|child_process)$'},
    },
  ];
}

/** The renderers' rules: what no renderer may reach. */
function rendererRules() {
  return [
    {
      // Renderer code (the renderer apps and the features) reaches the main process only through
      // its preload's bridge.
      name: 'renderers-do-not-import-electron',
      severity: 'error',
      from: {path: [DESKTOP_SHELL, FEATURE], pathNot: TEST_FILE},
      to: {path: '(?:^|/)node_modules/electron/', dependencyTypesNot: ['type-only']},
    },
    {
      // The renderer apps and the features reach the server only through @vibesys/transport or
      // the preload's bridge.
      name: 'web-opens-no-sockets-or-processes',
      severity: 'error',
      from: {path: [DESKTOP_SHELL, FEATURE], pathNot: TEST_FILE},
      to: {dependencyTypes: ['core'], path: '^(?:node:)?(?:net|child_process)$'},
    },
  ];
}

// Rules between packages (layers, runtimes, declared and exported imports, third-party
// confinement) have one home, `tools/scripts/check_package_graph.mjs`, which reads them from the
// manifests. These rules cover what it cannot see: modules inside a package, relative paths into
// another package, and how imports resolve.
/** @type {import('dependency-cruiser').IConfiguration} */
export default {
  forbidden: [
    {
      name: 'no-circular-dependencies',
      severity: 'error',
      from: {path: SCANNED},
      to: {circular: true},
    },
    {
      name: 'no-unresolvable-imports',
      severity: 'error',
      from: {path: SCANNED},
      // `bun:test` is a runtime builtin. dependency-cruiser cannot resolve the `exports`
      // subpath `dependency-cruiser/config-utl/*` (only the tooling under `tools` uses it),
      // although Node does. Nor does it resolve the type-only `knip` import of
      // `tools/knip.config.ts`; knip's own run loads that file.
      to: {
        couldNotResolve: true,
        pathNot: ['^bun:test$', '^dependency-cruiser/config-utl/', '^knip$'],
      },
    },
    {
      // Benchmarks, end-to-end specs, and workspace scripts are leaf tools: they consume the
      // packages, never the other way round, so a package build or test cannot depend on them.
      // Each tool may reach its own directory and no other's, which is what the `$1`
      // back-reference on `LEAF_TOOL_IMPORTER` says. Excluding every tool from `from` instead
      // would permit one tool directory to import another.
      name: 'tools-are-leaves',
      severity: 'error',
      from: {path: LEAF_TOOL_IMPORTER},
      to: {path: LEAF_TOOLS, pathNot: '^$1/'},
    },
    {
      // `tools/scripts/` is repository tooling (architecture checks). It reads package manifests
      // from disk, and does not import package code, so a package refactor cannot break the
      // gate that enforces the layering.
      name: 'scripts-do-not-import-package-code',
      severity: 'error',
      from: {path: '^tools/scripts/'},
      to: {path: PACKAGES},
    },
    {
      // Production code must not import a test file: test modules pull in `bun:test` and
      // fixtures, and are excluded from the build.
      name: 'production-code-does-not-import-tests',
      severity: 'error',
      from: {path: SCANNED, pathNot: TEST_FILE},
      to: {path: TEST_FILE},
    },
    {
      // Cross-runtime test helpers may use runtime adapters or deterministic scheduling seams
      // that shipping code must not reach. Keep them reachable only from test files.
      name: 'production-code-does-not-import-transport-test-support',
      severity: 'error',
      // Test support may build on test support, e.g. the testing entry re-exporting its Fakes, or
      // another package's harness building on the protocol fixtures.
      from: {path: SCANNED, pathNot: [TEST_FILE, TRANSPORT_TEST_ONLY, PACKAGE_TEST_SUPPORT]},
      to: {path: TRANSPORT_TEST_ONLY},
    },
    {
      // The store model checker and its seeded generator are test support for every frontend's
      // screen stores; only tests and other test support reach them.
      name: 'production-code-does-not-import-store-test-support',
      severity: 'error',
      from: {path: SCANNED, pathNot: [TEST_FILE, PACKAGE_TEST_SUPPORT]},
      to: {path: STORE_TEST_ONLY},
    },
    {
      // Another package's Fakes and test helpers are for tests: a package's shipping code that
      // imports them ships them. Test support may build on other test support (an entry's Fakes
      // over the store's model checker). Group 1 is the importer's own package, whose test
      // support the per-package rules above hold.
      name: 'production-code-does-not-import-other-packages-test-support',
      severity: 'error',
      from: {path: OWN_PACKAGE, pathNot: [TEST_FILE, ...ANY_TEST_SUPPORT]},
      to: {path: ANY_TEST_SUPPORT, pathNot: '^$1/'},
    },
    {
      name: 'workspace-packages-use-public-exports',
      severity: 'error',
      from: {path: OWN_PACKAGE},
      to: {
        path: PACKAGES,
        pathNot: '^$1/',
        dependencyTypes: ['local', 'localmodule'],
        dependencyTypesNot: ['aliased-tsconfig-paths'],
      },
    },
    ...desktopRules(),
    ...rendererRules(),
    ...featureRules(),
  ],
  options: {
    tsConfig: {fileName: 'tools/tsconfig.architecture.json'},
    doNotFollow: {path: 'node_modules'},
    preserveSymlinks: true,
    tsPreCompilationDeps: true,
    enhancedResolveOptions: {
      exportsFields: ['exports'],
      conditionNames: ['types', 'import', 'node', 'default'],
      extensions: ['.ts', '.tsx', '.mts', '.cts', '.js', '.jsx', '.mjs', '.cjs', '.d.ts', '.json'],
    },
    reporterOptions: {
      text: {highlightFocused: true},
    },
  },
};
