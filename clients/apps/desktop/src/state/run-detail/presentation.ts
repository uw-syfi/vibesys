/**
 * The stable, plain-data boundary a generic run screen consumes. The bounded reducer and its
 * projector own interpretation of the run log; this adapter only groups those typed facts for a
 * screen and derives the newest point of each already-ordered metric series. It never reads
 * records, protobuf Any values, display text or host-wide machine telemetry.
 */
import type {
  ProjectionAvailability,
  ProjectionSet,
  RunDetailFreshness,
  RunDetailProjection,
} from './types.js';

type AgentProjection = RunDetailProjection['agents']['values'][number];
type MetricProjection = RunDetailProjection['metrics']['values'][number];
type MetricPointProjection = MetricProjection['points'][number];
type ObjectiveProjection = RunDetailProjection['objectives']['values'][number];
type WorkstreamProjection = RunDetailProjection['workstreams']['values'][number];
type CorrectnessProjection = RunDetailProjection['correctness']['values'][number];
type UnprojectedSpan = RunDetailProjection['unprojectedSpans']['values'][number];

interface PresentationSection<T> {
  /** The run log owns this section; individual items retain their record source as well. */
  readonly source: ProjectionSet<T>['source'];
  readonly scope: ProjectionSet<T>['scope'];
  readonly freshness: RunDetailFreshness;
  readonly availability: ProjectionAvailability;
  readonly items: readonly T[];
}

interface MetricPresentation extends Omit<MetricProjection, 'points'> {
  readonly points: readonly MetricPointProjection[];
  /** No recorded point is null; it is never manufactured as zero. */
  readonly latest: MetricPointProjection | null;
}

interface RunPresentation {
  readonly id: string;
  readonly lifecycle: RunDetailProjection['lifecycle'] | null;
  readonly freshness: RunDetailFreshness;
}

interface OptimizationPresentation {
  readonly objectives: PresentationSection<ObjectiveProjection>;
  readonly metrics: PresentationSection<MetricPresentation>;
}

interface DiagnosticsPresentation {
  readonly truncation: RunDetailProjection['truncation'];
  readonly unprojectedSpans: PresentationSection<UnprojectedSpan>;
}

/**
 * Everything a generic run overview may render. Dynamic roles, lineage, usage, evidence and all
 * source/scope/freshness/availability values remain typed facts from `RunDetailProjection`.
 */
export interface RunDetailPresentation {
  readonly run: RunPresentation;
  readonly optimization: OptimizationPresentation;
  readonly agents: PresentationSection<AgentProjection>;
  readonly workstreams: PresentationSection<WorkstreamProjection>;
  readonly correctness: PresentationSection<CorrectnessProjection>;
  readonly diagnostics: DiagnosticsPresentation;
}

const sectionOf = <T>(section: ProjectionSet<T>): PresentationSection<T> => ({
  source: section.source,
  scope: section.scope,
  freshness: section.freshness,
  availability: section.availability,
  items: section.values,
});

const metricsOf = (
  section: RunDetailProjection['metrics'],
): PresentationSection<MetricPresentation> => ({
  source: section.source,
  scope: section.scope,
  freshness: section.freshness,
  availability: section.availability,
  items: section.values.map(series => ({
    ...series,
    latest: series.points.at(-1) ?? null,
  })),
});

/** Derive the one renderer-facing model from an already validated generic run projection. */
export function presentRunDetail(projection: RunDetailProjection): RunDetailPresentation {
  return {
    run: {
      id: projection.runId,
      lifecycle: projection.lifecycle ?? null,
      freshness: projection.freshness,
    },
    optimization: {
      objectives: sectionOf(projection.objectives),
      metrics: metricsOf(projection.metrics),
    },
    agents: sectionOf(projection.agents),
    workstreams: sectionOf(projection.workstreams),
    correctness: sectionOf(projection.correctness),
    diagnostics: {
      truncation: projection.truncation,
      unprojectedSpans: sectionOf(projection.unprojectedSpans),
    },
  };
}
