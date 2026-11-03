/**
 * The one Vitest configuration every Effect package runs with, passed by `run_vitest.mjs`.
 *
 * Tests resolve workspace packages to their source through the `@vibesys/source` export
 * condition, as `run_bun_tests.mjs` does for bun, so a test never depends on a built `dist/`.
 * Setting `resolve.conditions` replaces Vite's defaults, so the defaults are listed again.
 */

const SOURCE_CONDITION = '@vibesys/source';

export default {
  root: process.cwd(),
  resolve: {conditions: [SOURCE_CONDITION, 'module', 'browser', 'development|production']},
  ssr: {resolve: {conditions: [SOURCE_CONDITION, 'module', 'node', 'development|production']}},
};
