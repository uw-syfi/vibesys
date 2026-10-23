/**
 * Test support for every frontend's screen stores: the seeded generator and the model checker.
 * Reachable only from test files (`production-code-does-not-import-store-test-support`).
 */
export {type AwaitedTokens, checkModel, type StoreModel} from './model.js';
export {SEEDS, Seeded, seedsAtDepth} from './seeded.js';
