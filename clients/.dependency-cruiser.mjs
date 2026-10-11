import {fileURLToPath} from 'node:url';
import {pathAlternation, workspaceLayout} from './scripts/workspace_layout.mjs';

// Scanned sources: each package's `src/`, plus the non-shipping code that lives next to it (the
// replay harness `tui/dev`, the benchmarks, the web end-to-end specs) and the workspace's own
// tooling (`scripts`). Both sets come from the workspace layout rather than a name list, so a new
// package, or a new directory inside one, is covered by these rules on its first commit.
// `scripts/check_ts_architecture.mjs` derives the scan roots from the same place.
const layout = workspaceLayout(fileURLToPath(new URL('.', import.meta.url)));
const PACKAGE_DIRECTORIES = pathAlternation(layout.packages.map(({directory}) => directory));
const PACKAGES = `^(?:${PACKAGE_DIRECTORIES})/`;
// Capturing, so a rule can exclude the importer's own package with a `$1` back-reference.
const OWN_PACKAGE = `^(${PACKAGE_DIRECTORIES})/`;
const PACKAGE_SOURCES = `^(?:${PACKAGE_DIRECTORIES})/src/`;
const TOOLING = `^(?:${pathAlternation(layout.toolingDirectories)})/`;
const SCANNED = `${PACKAGES}|${TOOLING}`;
const TEST_FILE = '\\.test\\.[cm]?[jt]sx?$';
const TRANSPORT_TEST_ONLY =
  '^(?:transport|transport-node|contracts)/src/(?:test-support/|testing/)';
const CORE_STATE_TEST_ONLY = '^core-state/src/testing/';
// A package's own test-support directory (`desktop/src/testing/`): Fakes that only tests import.
const PACKAGE_TEST_SUPPORT = '^[^/]+/src/testing/';
// The replay harness is tooling, but it has its own rule below, with the reason it exists; the
// remaining tooling directories are plain leaf tools.
const DEV_HARNESS = 'tui/dev';
if (!layout.toolingDirectories.includes(DEV_HARNESS)) {
  throw new Error(`.dependency-cruiser.mjs: ${DEV_HARNESS} is no longer a tooling directory`);
}
const LEAF_TOOL_DIRECTORIES = pathAlternation(
  layout.toolingDirectories.filter(directory => directory !== DEV_HARNESS),
);
const LEAF_TOOLS = `^(?:${LEAF_TOOL_DIRECTORIES})/`;
// The importer of a `tools-are-leaves` edge: any file in a leaf tool directory, or any other file
// in a package. Group 1 is the importer's own leaf tool directory, so `to.pathNot: '^$1/'`
// excludes exactly that directory and no other tool's. Ordinary package code matches the second
// alternative, where group 1 does not participate, so `$1` is never substituted and the resulting
// `^$1/` matches no path: such an importer may reach no tool directory at all.
const LEAF_TOOL_IMPORTER = `^(${LEAF_TOOL_DIRECTORIES})/|${PACKAGES}`;

// The web run page (`web/src`) is feature-sliced, with layers that point down only:
// `app/` (composition) -> `features/<name>/` -> `entities/` (domain values and pure selectors)
// -> `platform/` (system services: the one backend adapter, lifecycle, storage) -> `shared/`
// (domain-free utilities). Inside a feature, `state/` is the pure core, `ui/` is presentational,
// and `effects/` is the shell. `docs/contributing/web-development.md` describes the layout.
const WEB = '^web/src/';
const WEB_LAYERS = ['app', 'features', 'entities', 'platform', 'shared'];
const TRANSPORT = ['^transport(?:-node)?/', '/node_modules/@vibesys/transport(?:-node)?/'];
// The protocol vocabulary, which presentational and domain-free code never names.
const CONTRACTS = ['^contracts/', '/node_modules/@vibesys/contracts/'];
const CORE_STATE = ['^core-state/', '/node_modules/@vibesys/core-state/'];
const WEB_BACKEND_ADAPTER = `${WEB}platform/backend\\.ts$`;
const WEB_FEATURE_PART = part => `${WEB}features/[^/]+/${part}/`;

function webRunPageRules() {
  const layerRules = WEB_LAYERS.slice(1).map((layer, index) => ({
    name: `web-${layer}-does-not-import-layers-above`,
    severity: 'error',
    from: {path: `${WEB}${layer}/`, pathNot: TEST_FILE},
    to: {path: `${WEB}(?:${WEB_LAYERS.slice(0, index + 1).join('|')})/`},
  }));
  return [
    ...layerRules,
    {
      name: 'web-layers-do-not-import-the-package-surface',
      severity: 'error',
      // `index.ts` is the package's public surface; nothing inside the layers reaches back to it.
      from: {path: `${WEB}(?:${WEB_LAYERS.join('|')})/`},
      to: {path: `${WEB}index\\.ts$`},
    },
    {
      // A feature never reaches another feature; cross-feature flows are wired in `app/`.
      name: 'web-features-are-isolated',
      severity: 'error',
      from: {path: `${WEB}features/([^/]+)/`},
      to: {path: `${WEB}features/`, pathNot: `${WEB}features/$1/`},
    },
    {
      // A feature's `index.ts` is its only public surface.
      name: 'web-features-are-entered-through-their-index',
      severity: 'error',
      from: {path: WEB, pathNot: `${WEB}features/`},
      to: {path: `${WEB}features/[^/]+/(?!index\\.ts$)`},
    },
    {
      // The functional core (domain values and each feature's reducers) does no I/O: no React,
      // no platform services, no transport runtime values. Generated types and core-state
      // are the inputs it folds.
      name: 'web-core-is-pure',
      severity: 'error',
      from: {path: [`${WEB}entities/`, WEB_FEATURE_PART('state')], pathNot: TEST_FILE},
      to: {
        path: [
          `${WEB}platform/`,
          '(?:^|/)node_modules/(?:react|react-dom|electron)/',
          ...TRANSPORT,
        ],
        dependencyTypesNot: ['type-only'],
      },
    },
    {
      name: 'web-feature-state-does-not-import-its-shell-or-view',
      severity: 'error',
      from: {path: WEB_FEATURE_PART('state'), pathNot: TEST_FILE},
      to: {path: [WEB_FEATURE_PART('ui'), WEB_FEATURE_PART('effects')]},
    },
    {
      // Presentational components render props and report events; `ui-no-*.grit` forbids their
      // I/O calls and effect hooks, and this forbids reaching anything that does I/O.
      name: 'web-feature-ui-does-not-depend-on-shell',
      severity: 'error',
      from: {path: WEB_FEATURE_PART('ui'), pathNot: TEST_FILE},
      to: {
        path: [
          WEB_FEATURE_PART('effects'),
          `${WEB}platform/`,
          ...TRANSPORT,
          ...CONTRACTS,
          '(?:^|/)node_modules/electron/',
        ],
      },
    },
    {
      name: 'web-feature-effects-do-not-import-ui',
      severity: 'error',
      from: {path: WEB_FEATURE_PART('effects'), pathNot: TEST_FILE},
      to: {path: WEB_FEATURE_PART('ui')},
    },
    {
      // One path to the backend: `platform/backend.ts` is the only module that imports
      // @vibesys/transport's runtime values (`web-fetch-only-in-backend.grit` holds `fetch` to it).
      name: 'web-transport-only-in-backend-adapter',
      severity: 'error',
      from: {path: WEB, pathNot: [TEST_FILE, WEB_BACKEND_ADAPTER]},
      to: {path: TRANSPORT, dependencyTypesNot: ['type-only']},
    },
    {
      // `shared/` is domain-free: no run, protocol, or connection vocabulary reaches it.
      name: 'web-shared-is-domain-free',
      severity: 'error',
      from: {path: `${WEB}shared/`, pathNot: TEST_FILE},
      to: {path: [...TRANSPORT, ...CONTRACTS, ...CORE_STATE]},
    },
  ];
}

// The desktop app is split by process, then sliced by feature, so each slice can move into its
// own package (`docs/contributing/desktop-connection.md`; roadmap #1878):
//
// - `src/main/` is the Electron main process, in layers that point down only: `app/`
//   (composition) -> `features/<name>/` -> `entities/` (pure projections) -> `platform/<name>/`
//   (system services: the connection manager, the settings) -> `shared/`. A stateful platform
//   slice is a pure `state/` core and a `shell/` that owns its processes, sockets, and timers.
// - `src/renderer/` is the renderer code (the welcome view and the sandboxed preloads); it reaches
//   the main process only through its preload's bridge.
// - The two processes share only workspace packages: the models in `@vibesys/entities` and the
//   typed messages in `@vibesys/ipc`, whose `neutral` runtime the package-graph checker holds.
const DESKTOP = '^desktop/src/';
const DESKTOP_MAIN = `${DESKTOP}main/`;
const DESKTOP_MAIN_LAYERS = ['app', 'features', 'entities', 'platform', 'shared'];
const DESKTOP_WELCOME = `${DESKTOP}renderer/welcome/`;
const DESKTOP_SLICE = '(?:features|platform)/[^/]+';
const DESKTOP_SLICE_SUPPORT = [TEST_FILE, `${DESKTOP}.*/testing/`];
const UI_RUNTIME = '(?:^|/)node_modules/(?:react|react-dom|electron)/';

function desktopRules() {
  const layerRules = DESKTOP_MAIN_LAYERS.slice(1).map((layer, index) => ({
    name: `desktop-${layer}-does-not-import-layers-above`,
    severity: 'error',
    from: {path: `${DESKTOP_MAIN}${layer}/`, pathNot: TEST_FILE},
    to: {path: `${DESKTOP_MAIN}(?:${DESKTOP_MAIN_LAYERS.slice(0, index + 1).join('|')})/`},
  }));
  return [
    ...layerRules,
    {
      name: 'desktop-renderer-does-not-import-main',
      severity: 'error',
      from: {path: `${DESKTOP}renderer/`},
      to: {path: DESKTOP_MAIN},
    },
    {
      name: 'desktop-main-does-not-import-renderer',
      severity: 'error',
      from: {path: DESKTOP_MAIN},
      to: {path: `${DESKTOP}renderer/`},
    },
    {
      // A functional core (a platform slice's `state/`, a welcome store) folds events into state
      // and requests: no Node or Electron, no shell, no view.
      name: 'desktop-state-is-pure',
      severity: 'error',
      from: {path: [`${DESKTOP_MAIN}.+/state/`, `${DESKTOP_WELCOME}state/`], pathNot: TEST_FILE},
      to: {
        path: [
          `${DESKTOP_MAIN}.+/shell/`,
          `${DESKTOP_WELCOME}(?:ui|effects)/`,
          UI_RUNTIME,
          ...TRANSPORT,
          ...CONTRACTS,
        ],
        dependencyTypesNot: ['type-only'],
      },
    },
    {
      name: 'desktop-state-has-no-node-runtime',
      severity: 'error',
      from: {
        path: [`${DESKTOP_MAIN}.+/state/`, `${DESKTOP_WELCOME}(?:state|ui)/`],
        pathNot: TEST_FILE,
      },
      to: {dependencyTypes: ['core']},
    },
    {
      // Only a platform slice's shell starts processes or opens sockets.
      name: 'desktop-only-shells-open-processes-and-sockets',
      severity: 'error',
      from: {path: DESKTOP, pathNot: [TEST_FILE, `${DESKTOP_MAIN}platform/[^/]+/shell/`]},
      to: {dependencyTypes: ['core'], path: '^(?:node:)?(?:net|child_process)$'},
    },
    {
      // A feature never reaches another feature; cross-feature flows are wired in `app/`.
      name: 'desktop-features-are-isolated',
      severity: 'error',
      from: {path: `${DESKTOP_MAIN}features/([^/]+)/`},
      to: {path: `${DESKTOP_MAIN}features/`, pathNot: `${DESKTOP_MAIN}features/$1/`},
    },
    {
      // A slice's `index.ts` is its only public surface (its test support aside). Group 1 is the
      // importer's own slice, which may reach all of itself.
      name: 'desktop-slices-are-entered-through-their-index',
      severity: 'error',
      from: {path: `${DESKTOP_MAIN}(${DESKTOP_SLICE})/`, pathNot: DESKTOP_SLICE_SUPPORT},
      to: {path: `${DESKTOP_MAIN}${DESKTOP_SLICE}/(?!index\\.ts$)`, pathNot: `${DESKTOP_MAIN}$1/`},
    },
    {
      name: 'desktop-composition-enters-slices-through-their-index',
      severity: 'error',
      from: {path: `${DESKTOP_MAIN}(?:app|entities|shared)/`, pathNot: DESKTOP_SLICE_SUPPORT},
      to: {path: `${DESKTOP_MAIN}${DESKTOP_SLICE}/(?!index\\.ts$)`},
    },
  ];
}

/** The renderers' rules: the welcome view's layers, and what no renderer may reach. */
function rendererRules() {
  return [
    {
      name: 'desktop-welcome-ui-does-not-depend-on-shell',
      severity: 'error',
      from: {path: `${DESKTOP_WELCOME}ui/`, pathNot: TEST_FILE},
      to: {
        path: [
          `${DESKTOP_WELCOME}effects/`,
          `${DESKTOP_WELCOME}main\\.tsx$`,
          ...TRANSPORT,
          ...CONTRACTS,
          '(?:^|/)node_modules/electron/',
        ],
      },
    },
    {
      name: 'desktop-welcome-effects-do-not-import-ui',
      severity: 'error',
      from: {path: `${DESKTOP_WELCOME}effects/`, pathNot: TEST_FILE},
      to: {path: [`${DESKTOP_WELCOME}ui/`, `${DESKTOP_WELCOME}main\\.tsx$`]},
    },
    {
      // Renderer code (the welcome view, the web run page) reaches the main process only
      // through its preload's bridge.
      name: 'renderers-do-not-import-electron',
      severity: 'error',
      from: {path: [DESKTOP_WELCOME, '^web/src/'], pathNot: TEST_FILE},
      to: {path: '(?:^|/)node_modules/electron/', dependencyTypesNot: ['type-only']},
    },
    {
      // The web run page reaches its server only through @vibesys/transport.
      name: 'web-opens-no-sockets-or-processes',
      severity: 'error',
      from: {path: '^web/src/', pathNot: TEST_FILE},
      to: {dependencyTypes: ['core'], path: '^(?:node:)?(?:net|child_process)$'},
    },
  ];
}

// Rules between packages (layers, runtimes, declared and exported imports, third-party
// confinement) have one home, `scripts/check_package_graph.mjs`, which reads them from the
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
      // subpath `dependency-cruiser/config-utl/*` (only the tooling under `scripts` uses it),
      // although Node does.
      to: {
        couldNotResolve: true,
        pathNot: ['^bun:test$', '^dependency-cruiser/config-utl/'],
      },
    },
    {
      // `tui/dev/` is the development replay harness. It is kept out of
      // `dist` by `rootDir: "src"`, but `tui/tsconfig.check.json` widens the root so
      // the harness itself is typechecked, and that widening makes a src -> dev
      // import typecheck cleanly. The build still rejects it (TS6059); this says
      // so at the layer that owns the rule rather than leaving it to a later step.
      // The harness may import any public workspace export, but no package code
      // (in any package, test files included) may import it.
      name: 'shipping-path-does-not-depend-on-dev-harness',
      severity: 'error',
      from: {path: PACKAGES, pathNot: `^${DEV_HARNESS}/`},
      to: {path: `^${DEV_HARNESS}/`},
    },
    {
      // Benchmarks, end-to-end specs, and workspace scripts are leaf tools: they consume the
      // packages, never the other way round, so a package build or test cannot depend on them.
      // Each tool may reach its own directory and no other's, which is what the `$1`
      // back-reference on `LEAF_TOOL_IMPORTER` says. Excluding every tool from `from` instead
      // would permit `web/e2e -> scripts` and `web/e2e -> tui/benchmarks`.
      name: 'tools-are-leaves',
      severity: 'error',
      from: {path: LEAF_TOOL_IMPORTER},
      to: {path: LEAF_TOOLS, pathNot: '^$1/'},
    },
    {
      // `scripts/` is repository tooling (architecture checks). It reads package manifests
      // from disk, and does not import package code, so a package refactor cannot break the
      // gate that enforces the layering.
      name: 'scripts-do-not-import-package-code',
      severity: 'error',
      from: {path: '^scripts/'},
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
      // Test support may build on itself, e.g. the testing entry re-exporting its Fakes.
      from: {path: SCANNED, pathNot: [TEST_FILE, TRANSPORT_TEST_ONLY]},
      to: {path: TRANSPORT_TEST_ONLY},
    },
    {
      // The store model checker and its seeded generator are test support for every frontend's
      // screen stores; only tests and other test support reach them.
      name: 'production-code-does-not-import-core-state-test-support',
      severity: 'error',
      from: {path: SCANNED, pathNot: [TEST_FILE, PACKAGE_TEST_SUPPORT]},
      to: {path: CORE_STATE_TEST_ONLY},
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
    // Layering inside `tui/src`. `docs/contributing/tui-architecture.md` gives the package
    // ownership of interaction state, effects, and OpenTUI rendering/input; these rules encode
    // that split over the current module structure. Test files are exempt: they wire layers
    // together on purpose.
    {
      // Widgets render the TUI-owned session model. Wire types stop in the model/controller
      // layer, so generated backend contracts cannot spread into presentation modules.
      name: 'tui-ui-does-not-depend-on-transport',
      severity: 'error',
      from: {path: '^tui/src/ui/', pathNot: TEST_FILE},
      to: {
        path: [
          '^(?:transport|transport-node|contracts)/',
          '/node_modules/@vibesys/(?:transport|transport-node|contracts)/',
        ],
      },
    },
    {
      // The replay harness consumes published workspace entry points. A relative import into any
      // package's src tree bypasses that interface, including an import into tui's own src tree.
      name: 'dev-cannot-deep-import-src',
      severity: 'error',
      from: {path: `^${DEV_HARNESS}/`},
      to: {path: PACKAGE_SOURCES, dependencyTypesNot: ['aliased-tsconfig-paths']},
    },
    {
      // OpenTUI is the rendering runtime. It lives in `ui/`, the composition root
      // (`index.ts`, `runtime.ts`), and the render-only self-test; state, command, and
      // controller modules stay renderer-free so they run under plain unit tests.
      name: 'tui-opentui-is-confined-to-ui-and-composition-root',
      severity: 'error',
      from: {
        path: '^tui/src/',
        pathNot: [TEST_FILE, '^tui/src/(?:ui/|index\\.ts$|runtime\\.ts$|self-test\\.ts$)'],
      },
      to: {path: '@opentui[+/]'},
    },
    {
      // State and command modules (`session-model`, `chat-menu`, `commands`, `palette-model`,
      // `diff-viewer`, ...) are below the effectful `session-controller` and the wiring that
      // creates it.
      name: 'tui-state-does-not-depend-on-controller-or-wiring',
      severity: 'error',
      from: {
        path: '^tui/src/',
        pathNot: [TEST_FILE, '^tui/src/ui/', '^tui/src/(?:runtime|index|launcher|self-test)\\.ts$'],
      },
      to: {path: '^tui/src/(?:session-controller|runtime|index|launcher)\\.ts$'},
    },
    {
      // State, command, and controller modules expose presentation-ready values to widgets; they
      // do not reach back into widget helpers. Shared pure presentation models live above ui/.
      name: 'state-does-not-import-src/ui',
      severity: 'error',
      from: {
        path: '^tui/src/',
        pathNot: [TEST_FILE, '^tui/src/ui/', '^tui/src/(?:runtime|index|launcher|self-test)\\.ts$'],
      },
      to: {path: '^tui/src/ui/'},
    },
    {
      // Widgets render state and call the controller; they do not construct the app or the
      // runtime that owns them.
      name: 'tui-ui-does-not-depend-on-composition-root',
      severity: 'error',
      from: {path: '^tui/src/ui/', pathNot: TEST_FILE},
      to: {path: '^tui/src/(?:runtime|index|launcher)\\.ts$'},
    },
    {
      // Widgets receive the controller as an argument and only name its interface; a value
      // import would let rendering code build or reach into the effect layer.
      name: 'tui-ui-uses-controller-by-type-only',
      severity: 'error',
      from: {path: '^tui/src/ui/', pathNot: TEST_FILE},
      to: {path: '^tui/src/session-controller\\.ts$', dependencyTypesNot: ['type-only']},
    },
    ...desktopRules(),
    ...rendererRules(),
    ...webRunPageRules(),
    {
      // `index.ts` (the frontend process) and `launcher.ts` (the `vibesys` bin) run on import,
      // so nothing may import them.
      name: 'tui-entrypoints-are-not-imported',
      severity: 'error',
      from: {path: '^tui/(?:src|dev|benchmarks)/', pathNot: TEST_FILE},
      to: {path: '^tui/src/(?:index|launcher)\\.ts$'},
    },
    {
      // The launcher runs under Node before the renderer exists and only spawns the server and
      // frontend processes. It shares theme names with the frontend and nothing else, so it
      // never loads OpenTUI, the controller, or the workspace packages.
      name: 'tui-launcher-stays-standalone',
      severity: 'error',
      from: {path: '^tui/src/launcher\\.ts$'},
      to: {
        path: [
          '^tui/src/(?!launcher\\.ts$|theme\\.ts$)',
          '^(?:transport|transport-node|contracts|core-state)/',
          '@opentui[+/]',
        ],
      },
    },
  ],
  options: {
    tsConfig: {fileName: 'tsconfig.architecture.json'},
    doNotFollow: {path: 'node_modules'},
    preserveSymlinks: true,
    tsPreCompilationDeps: true,
    enhancedResolveOptions: {
      conditionNames: ['types', 'import', 'node', 'default'],
      extensions: ['.ts', '.tsx', '.mts', '.cts', '.js', '.jsx', '.mjs', '.cjs', '.d.ts', '.json'],
    },
    reporterOptions: {
      text: {highlightFocused: true},
    },
  },
};
