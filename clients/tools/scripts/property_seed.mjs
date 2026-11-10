/**
 * The seed policy for fast-check property tests, the TypeScript half of the policy Hypothesis
 * (`derandomize` in the PR tier, a fresh draw nightly) and the Go property tests follow.
 *
 * `VIBESYS_PROPERTY_SEED` chooses the seed:
 *   - unset or empty: `FIXED_SEED`, so a pull-request run is a pure function of the code and a
 *     failure on an unrelated PR cannot come from a rare input;
 *   - a decimal integer: that seed, to replay a failure;
 *   - `random`: a fresh seed, which the nightly job uses to explore new inputs.
 *
 * Replaying a failure. fast-check prints `{ seed: N, path: "P" }` for a failing property. Rerun
 * with `VIBESYS_PROPERTY_SEED=N` and, to skip to the shrunk counterexample,
 * `VIBESYS_PROPERTY_PATH=P`; both reach every `fc.assert` that passes no `seed` or `path` of
 * its own. A test that passes its own `seed:` keeps it.
 *
 * One global seed is enough: fast-check derives each run's values from the seed and the run
 * index, so a property still sees many distinct inputs. Two properties over the same arbitrary
 * see the same stream, which costs a little variety within one run and none across nightly runs.
 */

export const SEED_VARIABLE = 'VIBESYS_PROPERTY_SEED';
export const PATH_VARIABLE = 'VIBESYS_PROPERTY_PATH';
export const FIXED_SEED = 4234;

const RANDOM = 'random';
const INTEGER = /^-?\d+$/;
const INT32_RANGE = 2 ** 32;

/**
 * The seed `environment` asks for, or a rejection naming the variable.
 *
 * @param {Record<string, string | undefined>} environment
 * @param {() => number} [random] a draw in [0, 1), injectable so the `random` case is testable
 * @returns {number} a 32-bit integer
 */
export function propertySeed(environment, random = Math.random) {
  const value = (environment[SEED_VARIABLE] ?? '').trim();
  if (value === '') return FIXED_SEED;
  if (value === RANDOM) return Math.floor(random() * INT32_RANGE) | 0;
  if (!INTEGER.test(value) || !Number.isSafeInteger(Number(value))) {
    throw new Error(`${SEED_VARIABLE}=${value}: expected a decimal integer or "${RANDOM}"`);
  }
  return Number(value) | 0;
}

/**
 * The fast-check global parameters `environment` asks for.
 *
 * @param {Record<string, string | undefined>} environment
 * @param {() => number} [random]
 */
export function propertyParameters(environment, random = Math.random) {
  const path = (environment[PATH_VARIABLE] ?? '').trim();
  return path === ''
    ? {seed: propertySeed(environment, random)}
    : {seed: propertySeed(environment, random), path};
}
