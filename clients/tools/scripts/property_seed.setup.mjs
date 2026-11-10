/**
 * Applies the property-test seed policy (`property_seed.mjs`) to fast-check.
 *
 * The shared runners load this file into every test process: `vitest.config.mjs` lists it in
 * `setupFiles` and `run_bun_tests.mjs` passes it as `--preload`. `check_test_runners.mjs`
 * requires every package that depends on fast-check to test through those runners.
 */
import fc from 'fast-check';
import {propertyParameters} from './property_seed.mjs';

fc.configureGlobal(propertyParameters(process.env));
