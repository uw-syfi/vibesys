/** @type {import('dependency-cruiser').IConfiguration} */
module.exports = {
  forbidden: [
    {
      name: 'no-circular-dependencies',
      severity: 'error',
      from: {path: '^(?:backend-client|core-state|tui)/'},
      to: {circular: true},
    },
    {
      name: 'no-unresolvable-imports',
      severity: 'error',
      from: {path: '^(?:backend-client|core-state|tui)/'},
      to: {couldNotResolve: true, pathNot: '^bun:test$'},
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
      name: 'shipping-path-does-not-depend-on-dev-harness',
      severity: 'error',
      from: {path: '^tui/src/'},
      to: {path: '^tui/dev/'},
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
      from: {path: '^([^/]+)/src/'},
      to: {
        path: '^(?:backend-client|core-state|tui)/',
        pathNot: '^$1/',
        dependencyTypes: ['local', 'localmodule'],
        dependencyTypesNot: ['aliased-tsconfig-paths'],
      },
    },
    {
      name: 'core-state-has-no-node-runtime',
      severity: 'error',
      from: {path: '^core-state/src/', pathNot: '\\.test\\.[cm]?[jt]sx?$'},
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
      from: {path: '^[^/]+/src/', pathNot: '\\.test\\.[cm]?[jt]sx?$'},
      to: {dependencyTypes: ['npm-no-pkg', 'npm-unknown']},
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
