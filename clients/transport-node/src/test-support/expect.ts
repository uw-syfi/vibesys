import {createRequire} from 'node:module';

/**
 * The same Jest-compatible assertion function in Node and Bun.
 *
 * `expect` publishes CommonJS and ESM entry points. Node loads either, while
 * Bun 1.4.2 currently resolves the ESM exports to `undefined`. Loading through
 * Node's standard CommonJS boundary gives both runtimes the actual function.
 * Production builds exclude this test-only module.
 */
const assertionModule = createRequire(import.meta.url)('expect') as typeof import('expect');

export const expect = assertionModule.expect;
