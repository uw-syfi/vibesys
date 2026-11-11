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
// Any package's test support, at any depth of its sources: the `testing/` entries that ship Fakes
// to other packages' tests, and the `test-support/` helpers.
// Two patterns, because the one-pattern `src/(?:.+/)?` nests quantifiers that dependency-cruiser's
// regular-expression safety check rejects.
const ANY_TEST_SUPPORT = [
  `^(?:${PACKAGE_DIRECTORIES})/src/(?:testing|test-support)/`,
  `^(?:${PACKAGE_DIRECTORIES})/src/.*/(?:testing|test-support)/`,
];
const LEAF_TOOL_DIRECTORIES = pathAlternation(layout.toolingDirectories);
// The end-to-end specs, and the support they share (`tools/e2e/`: the free port, the shards, the
// check that every drawn glyph is a bundled font).
const E2E_DIRECTORIES = pathAlternation(
  layout.toolingDirectories.filter(directory => directory.endsWith('/e2e')),
);
const E2E_SPECS = `^(?:${E2E_DIRECTORIES})/`;
const SHARED_E2E_SUPPORT = '^tools/e2e/';
const LEAF_TOOLS = `^(?:${LEAF_TOOL_DIRECTORIES})/`;
// The importer of a `tools-are-leaves` edge: any file in a leaf tool directory, or any other file
// in a package. Group 1 is the importer's own leaf tool directory, so `to.pathNot: '^$1/'`
// excludes exactly that directory and no other tool's. Ordinary package code matches the second
// alternative, where group 1 does not participate, so `$1` is never substituted and the resulting
// `^$1/` matches no path: such an importer may reach no tool directory at all.
const LEAF_TOOL_IMPORTER = `^(${LEAF_TOOL_DIRECTORIES})/|${PACKAGES}`;

// The desktop app's Electron main process is `apps/desktop/src/main`. Which package may reach
// which is the package-graph checker's; these rules hold the modules inside the app.
const DESKTOP_PROCESS_FILE = '^apps/desktop/src/main/process\\.ts$';
const DESKTOP_LOCAL_HOST_FILE = '^apps/desktop/src/main/local-host\\.ts$';
const MAIN_PROCESS = '^apps/desktop/src/main/';

function desktopRules() {
  return [
    {
      // Every process the main process starts goes through `ProcessSpawner` (main/process.ts), which
      // owns bounded retirement, so a child is never started anywhere else.
      name: 'desktop-main-starts-processes-only-in-process-ts',
      severity: 'error',
      from: {path: MAIN_PROCESS, pathNot: [TEST_FILE, DESKTOP_PROCESS_FILE]},
      to: {dependencyTypes: ['core'], path: '^(?:node:)?child_process$'},
    },
    {
      // The one socket the main process opens itself is the local gateway's (main/local-host.ts).
      name: 'desktop-main-opens-sockets-only-in-local-host-ts',
      severity: 'error',
      from: {path: MAIN_PROCESS, pathNot: [TEST_FILE, DESKTOP_LOCAL_HOST_FILE]},
      to: {dependencyTypes: ['core'], path: '^(?:node:)?net$'},
    },
  ];
}

// The Effect desktop app (`apps/desktop`, docs/contributing/clients-architecture.md, "State") is one
// package with a folder per role, because its parts share one set of services and one release
// (a package is justified later only by a second real consumer, generated code, or a measured
// build problem). Which package may reach which is the package-graph checker's, and its entries
// carry the runtimes; these rules hold the folders inside it:
//
//   state    plain folds and views. Imports nothing outside itself at run time and never Effect.
//   core     Effect services, transport, atoms. May import state.
//   ui       React screens. May import core and state.
//   main     Electron main process; preload: the sandboxed bridge. Never import ui.
//   testing  doubles at process, network and clock boundaries. Only tests import it.
const DESKTOP_APP = '^apps/desktop/src/';
const DESKTOP_PART = part => `^apps/desktop/src/${part}/`;
const EFFECT_RUNTIME = '(?:^|/)node_modules/(?:effect|@effect/[^/]+)/';
// The generated orchestrator protocol: plain message and enum definitions with no I/O. A fold
// reads its enums (`ItemStatus`, `TurnStatus`) at run time, and a mirrored table would repeat them.
const ORCHESTRATOR_PROTO = [
  '^libs/orchestrator-proto/',
  '/node_modules/@vibesys/orchestrator-proto/',
];

// Generated host/store messages plus their pure validators and descriptor resolver. Run-detail
// folds decode persisted record bytes and dynamic descriptors; the exception names only the
// foundation package that owns those generated contracts.
const HOST_STORE_PROTO = ['^libs/proto/', '/node_modules/@vibesys/proto/'];

// The design system's token tables: plain data and pure functions with no DOM (`@vibesys/ui/tokens`).
// A fold reads the theme table at run time (the appearance fold, which resolves the theme a mode
// shows), and a mirrored table would repeat it.
const DESIGN_TOKENS = ['^libs/ui/src/tokens/', '/node_modules/@vibesys/ui/(?:src|dist)/tokens/'];

function desktopAppRules() {
  return [
    {
      // A fold is a function from data to data that a test calls directly. Only a type, which the
      // compiler erases, may name another module, except generated protocols whose enums,
      // validators and descriptor resolver a fold reads at run time, and the design tokens,
      // whose theme table the appearance fold reads.
      name: 'desktop-state-imports-nothing-outside-itself',
      severity: 'error',
      from: {path: DESKTOP_PART('state'), pathNot: TEST_FILE},
      to: {
        pathNot: [
          DESKTOP_PART('state'),
          ...ORCHESTRATOR_PROTO,
          ...HOST_STORE_PROTO,
          ...DESIGN_TOKENS,
        ],
        dependencyTypesNot: ['type-only'],
      },
    },
    {
      // Not even for a type: a fold that names an Effect type is on its way to running one.
      name: 'desktop-state-never-imports-effect',
      severity: 'error',
      from: {path: DESKTOP_PART('state'), pathNot: TEST_FILE},
      to: {path: EFFECT_RUNTIME},
    },
    {
      name: 'desktop-core-does-not-reach-the-shells',
      severity: 'error',
      from: {path: DESKTOP_PART('core'), pathNot: TEST_FILE},
      to: {path: [DESKTOP_PART('ui'), DESKTOP_PART('main'), DESKTOP_PART('preload')]},
    },
    {
      // A screen reads services and folds; the main process and the preload reach it only through
      // the bridge.
      name: 'desktop-ui-does-not-reach-main-or-preload',
      severity: 'error',
      from: {path: DESKTOP_PART('ui'), pathNot: TEST_FILE},
      to: {path: [DESKTOP_PART('main'), DESKTOP_PART('preload')]},
    },
    {
      name: 'desktop-main-and-preload-do-not-import-ui',
      severity: 'error',
      from: {path: [DESKTOP_PART('main'), DESKTOP_PART('preload')], pathNot: TEST_FILE},
      to: {path: DESKTOP_PART('ui')},
    },
    {
      // The main process loads a preload by path into a page; it never runs one itself.
      name: 'desktop-main-does-not-import-the-preload',
      severity: 'error',
      from: {path: DESKTOP_PART('main'), pathNot: TEST_FILE},
      to: {path: DESKTOP_PART('preload')},
    },
    {
      // A sandboxed preload's `require` reaches Electron only (`preload-sandbox` in the
      // package-graph checker); types are erased and may name the app's own.
      name: 'desktop-preload-loads-only-electron',
      severity: 'error',
      from: {path: DESKTOP_PART('preload'), pathNot: TEST_FILE},
      to: {pathNot: '(?:^|/)node_modules/electron/', dependencyTypesNot: ['type-only']},
    },
    {
      // The page reaches the main process only through the preload's bridge.
      name: 'desktop-page-code-does-not-import-electron',
      severity: 'error',
      from: {
        path: [DESKTOP_PART('state'), DESKTOP_PART('core'), DESKTOP_PART('ui')],
        pathNot: TEST_FILE,
      },
      to: {path: '(?:^|/)node_modules/electron/', dependencyTypesNot: ['type-only']},
    },
    {
      name: 'desktop-page-code-opens-no-sockets-or-processes',
      severity: 'error',
      from: {
        path: [DESKTOP_PART('state'), DESKTOP_PART('core'), DESKTOP_PART('ui')],
        pathNot: TEST_FILE,
      },
      to: {dependencyTypes: ['core'], path: '^(?:node:)?(?:net|child_process)$'},
    },
    {
      // A feature (a subfolder of `state`, `core`, `ui` or `testing` named for what it does) is
      // reached only by the shell, which composes the features, and by its own folders. Two
      // features that need one another's facts meet in the shell, which passes them as props.
      name: 'desktop-features-do-not-import-each-other',
      severity: 'error',
      from: {
        path: '^apps/desktop/src/(?:state|core|ui)/(orchestrator|settings)/',
        pathNot: TEST_FILE,
      },
      to: {
        path: '^apps/desktop/src/(?:state|core|ui|testing)/(?:orchestrator|settings|shell)/',
        pathNot: '^apps/desktop/src/(?:state|core|ui|testing)/$1/',
      },
    },
    {
      // `agents` is a feature built on the orchestrator's state and words, so it may read
      // `orchestrator`; it meets every other feature in the shell.
      name: 'desktop-agents-feature-reads-only-orchestrator',
      severity: 'error',
      from: {path: '^apps/desktop/src/(?:state|core|ui)/agents/', pathNot: TEST_FILE},
      to: {
        path: '^apps/desktop/src/(?:state|core|ui|testing)/(?:settings|shell|cost|catch-up)/',
      },
    },
    {
      name: 'desktop-production-code-does-not-import-testing',
      severity: 'error',
      from: {path: DESKTOP_APP, pathNot: [TEST_FILE, DESKTOP_PART('testing')]},
      to: {path: DESKTOP_PART('testing')},
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
      to: {path: LEAF_TOOLS, pathNot: ['^$1/', SHARED_E2E_SUPPORT]},
    },
    {
      // The exception above, closed again: the shared end-to-end support is for the end-to-end
      // specs and the workspace's own `tools/`, not for package code or the packages' benchmarks.
      name: 'shared-e2e-support-is-for-e2e-specs',
      severity: 'error',
      from: {pathNot: [E2E_SPECS, '^tools/']},
      to: {path: SHARED_E2E_SUPPORT},
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
    ...desktopAppRules(),
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
