/** Declared series remain chartable even when an old producer recorded no idea owner. */
import type {Span} from '@vibesys/contracts';
import type {Candidate, ObjectiveAxis} from '@vibesys/run-state/candidates';
import type {StoreView} from '@vibesys/run-state/skeleton';
import type {PerformanceFold, PerformancePoint} from './performance.js';

export function skeletonPerformance(
  view: StoreView,
  spans: readonly Span[],
  candidates: readonly Candidate[],
  objectives: readonly ObjectiveAxis[],
): ReadonlyMap<string, PerformanceFold> {
  const byId = new Map(spans.map(span => [span.id, span]));
  const owners = new Map(
    candidates.flatMap(idea => idea.attempts.map(attempt => [attempt.span, idea] as const)),
  );
  function owner(id: string | undefined): Candidate | undefined {
    const seen = new Set<string>();
    while (id && !seen.has(id)) {
      seen.add(id);
      const idea = owners.get(id);
      if (idea) return idea;
      id = byId.get(id)?.parent ?? undefined;
    }
    return undefined;
  }
  return new Map(
    view.metrics.map(history => {
      const direction = objectives.find(axis => axis.name === history.series)?.direction;
      const points: PerformancePoint[] = [];
      const best: {at: number; value: number}[] = [];
      const ordered = history.points
        .flatMap(version => (version.point ? [{seq: version.seq, point: version.point}] : []))
        .sort((a, b) => a.point.clock - b.point.clock);
      for (const {seq, point} of ordered) {
        const span = byId.get(point.span ?? '');
        const idea = owner(point.span);
        const standing = best.at(-1)?.value;
        const sets = improves(point.value, standing, direction);
        if (sets) best.push({at: point.clock, value: point.value});
        points.push({
          id: idea?.id ?? `metric:${history.series}:${seq}`,
          key: `metric:${history.series}:${seq}`,
          unowned: idea === undefined,
          title: idea?.title ?? (span?.label?.title || history.series),
          at: point.clock,
          value: point.value,
          result: span?.status === 'failed' ? 'failed' : 'passed',
          best: sets,
        });
      }
      return [history.series, {points, best}];
    }),
  );
}

function improves(
  value: number,
  standing: number | undefined,
  direction: 'min' | 'max' | undefined,
) {
  return standing === undefined || (direction === 'min' ? value < standing : value > standing);
}
