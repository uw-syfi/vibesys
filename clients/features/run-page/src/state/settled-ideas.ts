/**
 * The ideas with the figures the framework's comparisons settled on. A gate span states the
 * reading of the run that answered it, but an idea is judged on the comparison of its measurement
 * with its base's (`measurement_compared`): the mean of every run it earned, so a close call that
 * earned more runs reads as its mean, not as one of them. This joins the two: each measured attempt
 * whose revision was compared takes the comparison's candidate value as its value, the ratio of
 * that value to the input's, and the comparison itself (`BaseComparison`), so an idea whose input
 * was never measured still has the ratio the framework made, against the base it names.
 *
 * Pure over the candidates and the core's comparisons. Unchanged ideas keep their identity, so the
 * readers' caches and the rows' flash on change keep working.
 */
import type {CoreState, MeasurementComparison} from '@vibesys/run-state';
import type {
  Candidate,
  CandidateAttempt,
  CandidateMeasurement,
  CandidateTree,
} from '@vibesys/run-state/candidates';

type Comparisons = CoreState['comparisons'];

/** Whether `comparison` states the value of `measurement`'s headline reading. */
function statesHeadline(measurement: CandidateMeasurement, comparison: MeasurementComparison) {
  const headline = measurement.readings[0];
  return headline === undefined || headline.name === comparison.metric;
}

function settledMeasurement(
  measurement: CandidateMeasurement,
  comparison: MeasurementComparison,
): CandidateMeasurement {
  const input = measurement.inputReadings[0];
  const headline = measurement.readings[0];
  const vsInput =
    input !== undefined &&
    input.value !== 0 &&
    (headline === undefined || input.name === headline.name)
      ? comparison.candidateValue / input.value
      : measurement.vsBaseline;
  return {
    ...measurement,
    metricValue: comparison.candidateValue,
    vsBaseline: vsInput,
    readings: measurement.readings.map((reading, at) =>
      at === 0 ? {...reading, value: comparison.candidateValue} : reading,
    ),
    against: {
      ratio: comparison.ratio,
      baseCommit: comparison.baseCommit,
      baseValue: comparison.baseValue,
      verdict: comparison.verdict,
    },
  };
}

const ATTEMPTS = new WeakMap<
  CandidateAttempt,
  {comparison: MeasurementComparison; settled: CandidateAttempt}
>();

function settledAttempt(attempt: CandidateAttempt, comparisons: Comparisons): CandidateAttempt {
  const measurement = attempt.measurement;
  if (measurement === null || measurement.metricValue === null || attempt.revision === null) {
    return attempt;
  }
  const comparison = comparisons[attempt.revision];
  if (comparison === undefined || !statesHeadline(measurement, comparison)) return attempt;
  const known = ATTEMPTS.get(attempt);
  if (known?.comparison === comparison) return known.settled;
  const settled = {...attempt, measurement: settledMeasurement(measurement, comparison)};
  ATTEMPTS.set(attempt, {comparison, settled});
  return settled;
}

const CANDIDATES = new WeakMap<
  Candidate,
  {comparisons: readonly (MeasurementComparison | undefined)[]; settled: Candidate}
>();

function settledCandidate(item: Candidate, comparisons: Comparisons): Candidate {
  const attempts = item.attempts.map(attempt => settledAttempt(attempt, comparisons));
  if (attempts.every((attempt, at) => attempt === item.attempts[at])) return item;
  const used = item.attempts.map(attempt =>
    attempt.revision === null ? undefined : comparisons[attempt.revision],
  );
  const known = CANDIDATES.get(item);
  if (known?.comparisons.every((comparison, at) => comparison === used[at]) === true) {
    return known.settled;
  }
  // The idea's measurement is its latest measured attempt's, so it follows that attempt.
  const shown = item.attempts.findIndex(attempt => attempt.measurement === item.measurement);
  const settled: Candidate = {
    ...item,
    attempts,
    measurement: shown < 0 ? item.measurement : (attempts[shown]?.measurement ?? item.measurement),
  };
  CANDIDATES.set(item, {comparisons: used, settled});
  return settled;
}

const TREES = new WeakMap<CandidateTree, {comparisons: Comparisons; settled: CandidateTree}>();

/** `tree` with each measured idea's figures settled by `comparisons`; `tree` itself when none apply. */
export function settleIdeas(tree: CandidateTree, comparisons: Comparisons): CandidateTree {
  if (Object.keys(comparisons).length === 0) return tree;
  const known = TREES.get(tree);
  if (known?.comparisons === comparisons) return known.settled;
  const candidates = tree.candidates.map(item => settledCandidate(item, comparisons));
  const settled = candidates.every((item, at) => item === tree.candidates[at])
    ? tree
    : {...tree, candidates};
  TREES.set(tree, {comparisons, settled});
  return settled;
}
