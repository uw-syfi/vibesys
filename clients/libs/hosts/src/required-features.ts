/**
 * The helper features this app needs a machine's VibeSys to have: the one rule that says whether a
 * machine can be used from here. New run (typed run options, the Ready to start checks, the
 * examples) and the Run screen (a run's history, and its summary with the run trace) use only what
 * these name, so a machine without one is "needs an update" everywhere it is shown: its status in
 * Machines, Where and Review in New run, and the run it holds. A feature enters the list when a
 * screen starts to depend on it; a reply that gains a field gets a feature of its own, so an older
 * helper is refused before it answers in a shape this app cannot read.
 */
import type {HelperFeature} from '@vibesys/contracts/host-helper';

export const REQUIRED_HELPER_FEATURES: readonly HelperFeature[] = [
  'run_options',
  'preflight',
  'examples',
  'run_history',
  'run_summary',
  'run_trace',
];

/**
 * The required features a helper that agreed to `features` in `hello` lacks; none while they are
 * not known yet (`null`).
 */
export function missingRequiredFeatures(
  features: readonly HelperFeature[] | null,
): readonly HelperFeature[] {
  if (features === null) return [];
  return REQUIRED_HELPER_FEATURES.filter(feature => !features.includes(feature));
}
