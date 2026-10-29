/** Render core's committed decisions. A reading alone carries no passed or best claim. */
import type {Span} from '@vibesys/contracts';
import type {RunLivenessHint} from '@vibesys/run-skeleton';
import type {Candidate} from '@vibesys/run-state/candidates';
import {
  type PerformanceDecision,
  type PerformanceReading,
  readPerformance,
  type StoreView,
} from '@vibesys/run-state/skeleton';
import type {PerformanceFold, PerformancePoint} from './performance.js';

type Owner = (span: string | undefined) => Candidate | undefined;

export function skeletonPerformance(
  view: StoreView,
  spans: readonly Span[],
  candidates: readonly Candidate[],
  head?: RunLivenessHint['head'],
): ReadonlyMap<string, PerformanceFold> {
  const byId = new Map(spans.map(span => [span.id, span]));
  const owners = new Map(
    candidates.flatMap(idea => idea.attempts.map(attempt => [attempt.span, idea] as const)),
  );
  const owner: Owner = start => {
    const seen = new Set<string>();
    let id = start;
    while (id && !seen.has(id)) {
      seen.add(id);
      const idea = owners.get(id);
      if (idea) return idea;
      id = byId.get(id)?.parent ?? undefined;
    }
    return undefined;
  };
  const facts = readPerformance(view, head);
  return new Map(
    view.metrics.map(history => [
      history.series,
      seriesPerformance(history, facts.get(history.series), owner),
    ]),
  );
}

function seriesPerformance(
  history: StoreView['metrics'][number],
  facts: PerformanceReading | undefined,
  owner: Owner,
): PerformanceFold {
  const decisions = facts?.points ?? new Map<bigint, PerformanceDecision>();
  const ordered = history.points
    .flatMap(version => (version.point ? [{seq: version.seq, point: version.point}] : []))
    .sort((a, b) => a.point.clock - b.point.clock);
  const points = ordered.map(({seq, point}) =>
    pointView(seq, point, decisions.get(seq), owner(point.span)),
  );
  return {
    points,
    best: points.filter(point => point.best).map(point => ({at: point.at, value: point.value})),
    value: facts?.value,
    input: facts?.input,
    refusal: facts?.refusal ?? (facts === undefined ? 'Performance facts unavailable' : undefined),
  };
}

function pointView(
  seq: bigint,
  point: NonNullable<StoreView['metrics'][number]['points'][number]['point']>,
  fact: PerformanceDecision | undefined,
  idea: Candidate | undefined,
): PerformancePoint {
  return {
    id: idea?.id ?? `metric:${point.series}:${seq}`,
    key: `metric:${point.series}:${seq}`,
    unowned: idea === undefined,
    title: idea?.title ?? point.series,
    at: point.clock,
    value: point.value,
    result: fact?.result ?? 'pending',
    outcomeWord: fact?.word ?? 'Outcome unavailable',
    best: fact?.best === true,
  };
}
