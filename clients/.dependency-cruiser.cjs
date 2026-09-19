// Scanned sources: each package's `src/`, plus the non-shipping code that lives next to it: the
// replay harness (`tui/dev`), benchmarks, and the workspace's own tooling (`scripts`).
const PACKAGES = '^(?:backend-client|core-state|tui)/';
const TOOLING = '^(?:tui/dev|tui/benchmarks|core-state/bench|scripts)/';
const SCANNED = `${PACKAGES}|${TOOLING}`;
const TEST_FILE = '\\.test\\.[cm]?[jt]sx?$';

/** @type {import('dependency-cruiser').IConfiguration} */
module.exports = {
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
      name: 'backend-client-is-lowest-layer',
      severity: 'error',
      from: {path: '^backend-client/src/'},
      to: {
        path: ['^(?:core-state|tui)/', '/node_modules/@vibesys/(?:core-state|tui)/'],
      },
    },
    {
      // `tui/dev/` is the development replay harness. It is kept out of
      // `dist` by `rootDir: "src"`, but `tsconfig.check.json` widens the root so
      // the harness itself is typechecked, and that widening makes a src -> dev
      // import typecheck cleanly. The build still rejects it (TS6059); this says
      // so at the layer that owns the rule rather than leaving it to a later step.
      // The harness may import any public workspace export, but no package code
      // (in any package, test files included) may import it.
      name: 'shipping-path-does-not-depend-on-dev-harness',
      severity: 'error',
      from: {path: PACKAGES, pathNot: '^tui/dev/'},
      to: {path: '^tui/dev/'},
    },
    {
      // Benchmarks and workspace scripts are leaf tools: they consume the packages, never the
      // other way round, so a package build or test cannot depend on them.
      name: 'tools-are-leaves',
      severity: 'error',
      from: {path: PACKAGES, pathNot: '^(?:tui/benchmarks|core-state/bench)/'},
      to: {path: '^(?:tui/benchmarks|core-state/bench|scripts)/'},
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
      name: 'core-state-does-not-depend-on-tui',
      severity: 'error',
      from: {path: '^core-state/src/'},
      to: {path: ['^tui/', '/node_modules/@vibesys/tui/']},
    },
    {
      name: 'workspace-packages-use-public-exports',
      severity: 'error',
      from: {path: '^([^/]+)/(?:src|dev|benchmarks|bench)/'},
      to: {
        path: '^(?:backend-client|core-state|tui)/',
        pathNot: '^$1/',
        dependencyTypes: ['local', 'localmodule'],
        dependencyTypesNot: ['aliased-tsconfig-paths'],
      },
    },
    {
      // The public entry points are the `exports` of each package, mapped to source by
      // `tsconfig.architecture.json`, so a public import never resolves into `node_modules`.
      // A specifier such as `@vibesys/x/dist/...` or `@vibesys/x/src/...` does: it reaches
      // build output or private modules. (When the package declares `exports`, such a
      // specifier is also unresolvable, which `no-unresolvable-imports` reports.)
      name: 'workspace-packages-have-no-deep-imports',
      severity: 'error',
      from: {path: SCANNED},
      to: {path: '(?:^|/)node_modules/@vibesys/'},
    },
    {
      name: 'core-state-has-no-node-runtime',
      severity: 'error',
      from: {path: '^core-state/src/', pathNot: TEST_FILE},
      to: {dependencyTypes: ['core']},
    },
    {
      name: 'core-state-has-no-ui-runtime',
      severity: 'error',
      from: {path: '^core-state/src/'},
      to: {path: '@opentui[+/]'},
    },
    {
      name: 'production-dependencies-are-declared',
      severity: 'error',
      // Applies to the tooling too (harness, benchmarks, scripts): they must declare what they
      // import, though they may import any public workspace export.
      from: {
        path: `^[^/]+/src/|${TOOLING}`,
        pathNot: '^[^/]+/src/.*\\.test\\.[cm]?[jt]sx?$',
      },
      to: {dependencyTypes: ['npm-no-pkg', 'npm-unknown']},
    },
    // Layering inside `tui/src`. `docs/contributing/tui-architecture.md` gives the package
    // ownership of interaction state, effects, and OpenTUI rendering/input; these rules encode
    // that split over the current module structure. Test files are exempt: they wire layers
    // together on purpose.
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
          '^tui/src/(?!launcher\\.ts$|ui/theme\\.ts$)',
          '^(?:backend-client|core-state)/',
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
