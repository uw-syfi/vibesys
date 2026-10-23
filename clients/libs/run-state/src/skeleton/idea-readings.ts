/** An idea displays the reading core selected, even if a later check measured nothing. */
import {Direction, type MetricPoint, type MetricSeriesDecl, type RunHead} from '@vibesys/proto';
import type {Candidate, CandidateMeasurement, CandidateTree} from '../candidates/types.js';

export function coreIdeaKey(idea: Candidate): string {
  return idea.hypothesis === null ? `span:${idea.id}` : `hypothesis:${idea.hypothesis}`;
}

export function coreIdeaMeasurements(
  tree: CandidateTree,
  head: RunHead | undefined,
  seq: bigint,
): CandidateTree {
  if (!head?.performanceStated || head.committedSeq !== seq) return tree;
  const series = head.performance[0];
  const declaration = head.objective?.series;
  if (!series || !declaration) return tree;
  const points = new Map(series.points.map(point => [point.measurementSeq, point.point]));
  const decisions = new Map(series.ideas.map(idea => [idea.idea, idea]));
  return {
    ...tree,
    candidates: tree.candidates.map(idea => {
      const selected = decisions.get(coreIdeaKey(idea))?.measurementSeq;
      const point = selected === undefined ? undefined : points.get(selected);
      if (!point) return idea;
      const measurement = coreMeasurement(point, declaration, series.inputValue, idea.measurement);
      return {...idea, measurement};
    }),
  };
}

function coreMeasurement(
  point: MetricPoint,
  declaration: MetricSeriesDecl,
  input: number | undefined,
  prior: CandidateMeasurement | null,
): CandidateMeasurement {
  const direction = declaration.direction === Direction.MIN ? 'min' : 'max';
  const reading = {
    name: point.series,
    value: point.value,
    unit: declaration.unit,
    direction,
  } as const;
  return {
    span: point.span ?? prior?.span ?? '',
    at: point.clock,
    metricValue: point.value,
    vsBaseline: input === undefined || input === 0 ? null : point.value / input,
    against: prior?.against ?? null,
    readings: [reading, ...(prior?.readings ?? []).filter(row => row.name !== point.series)],
    inputReadings:
      input === undefined
        ? []
        : [
            {...reading, value: input},
            ...(prior?.inputReadings ?? []).filter(row => row.name !== point.series),
          ],
    verdict: prior?.verdict ?? null,
    failed: prior?.failed ?? null,
  };
}
