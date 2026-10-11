/**
 * Test support for every frontend's screen stores: the seeded generator and the model checker.
 * Reachable only from test files (`production-code-does-not-import-core-state-test-support`).
 */
export {type AwaitedTokens, checkModel, type StoreModel} from './model.js';
export {SEEDS, Seeded} from './seeded.js';
