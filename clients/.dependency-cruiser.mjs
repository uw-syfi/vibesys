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
const TOOLING = `^(?:${pathAlternation(layout.toolingDirectories)})/`;
const SCANNED = `${PACKAGES}|${TOOLING}`;
const TEST_FILE = '\\.test\\.[cm]?[jt]sx?$';

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

function packagesAbove(directory) {
  const above = layout.packages.filter(
    workspacePackage => workspacePackage.directory !== directory,
  );
  return [
    `^(?:${pathAlternation(above.map(workspacePackage => workspacePackage.directory))})/`,
    `/node_modules/(?:${pathAlternation(above.map(workspacePackage => workspacePackage.name))})/`,
  ];
}

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
      // `backend-client` is the lowest layer, so it may not reach any other package: the set is
      // derived, because a rule that names the layers above it goes stale when one is added.
      name: 'backend-client-is-lowest-layer',
      severity: 'error',
      from: {path: '^backend-client/src/'},
      to: {path: packagesAbove('backend-client')},
    },
    {
      name: 'web-does-not-depend-on-tui',
      severity: 'error',
      from: {path: '^web/src/'},
      to: {path: ['^tui/', '/node_modules/@vibesys/tui/']},
    },
    {
      name: 'tui-does-not-depend-on-web',
      severity: 'error',
      from: {path: '^tui/'},
      to: {path: ['^web/', '/node_modules/@vibesys/web/']},
    },
    {
      // The shell loads the app over HTTP (home server or Vite) and shares no code with the
      // client packages, so a client refactor cannot reach its process and security code.
      name: 'desktop-is-standalone',
      severity: 'error',
      from: {path: '^desktop/'},
      to: {path: ['^(?:backend-client|core-state|tui|web)/', '/node_modules/@vibesys/']},
    },
    {
      // The reverse of `desktop-is-standalone`: the clients run in a browser and never load shell code.
      name: 'clients-do-not-depend-on-desktop',
      severity: 'error',
      from: {path: '^(?:backend-client|core-state|tui|web)/'},
      to: {path: ['^desktop/', '/node_modules/@vibesys/desktop/']},
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
      name: 'core-state-does-not-depend-on-tui',
      severity: 'error',
      from: {path: '^core-state/src/'},
      to: {path: ['^tui/', '/node_modules/@vibesys/tui/']},
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
      // The default (`.`) entry must bundle for a browser, so nothing it reaches
      // may import a Node builtin. Only `backend-client/src/node/` may: the
      // node-socket transport lives there, behind the `./node` export. Everything
      // above that seam (protocol, folds, backoff, request policy, the transport
      // interface) stays runtime-neutral. This mirrors core-state-has-no-node-runtime.
      name: 'backend-client-neutral-has-no-node-runtime',
      severity: 'error',
      from: {path: '^backend-client/src/', pathNot: ['^backend-client/src/node/', TEST_FILE]},
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
      // Applies to the tooling too (harness, benchmarks, end-to-end specs, scripts): they must
      // declare what they import, though they may import any public workspace export.
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
