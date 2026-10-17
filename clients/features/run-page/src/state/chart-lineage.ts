/**
 * The performance chart's connectors: from each passed, measured idea's point to its base's point,
 * the input standing as the baseline at the start of the run. An idea that failed, runs or has no
 * point gets none; an idea whose base has no point (failed, or not measured) is connected to the
 * nearest ancestor that has one. The best idea's chain is emphasized (`strong`), and the chain of
 * the idea the reader points at, selected or opened is lit (`lit`).
 *
 * A pure projection of the candidates and the points the chart already draws; it adds no point and
 * moves none, so connectors never change the chart's axes.
 */
import type {Candidate} from '@vibesys/run-state/candidates';

/** Where an edge starts: a point's id, or a place on the chart (the input's baseline). */
type EdgeFrom = string | {readonly x: number; readonly y: number};

export interface ChartEdgeView {
  readonly from: EdgeFrom;
  readonly to: string;
  readonly emphasis?: 'strong' | 'lit';
  /** Its point is after the scrub position. */
  readonly faint: boolean;
}

/** What the connectors read of the chart's points: which are drawn, which passed, which are faint. */
export interface EdgePoint {
  readonly id: string;
  readonly passed: boolean;
  readonly faint: boolean;
}

/** The nearest ancestor of `item` that has a passed point, or null (the input). */
function baseWithPoint(
  item: Candidate,
  byId: ReadonlyMap<string, Candidate>,
  passed: ReadonlySet<string>,
): string | null {
  const seen = new Set<string>();
  for (let at = item.parent; at !== null && !seen.has(at); at = byId.get(at)?.parent ?? null) {
    if (passed.has(at)) return at;
    seen.add(at);
  }
  return null;
}

export function chartEdges(
  candidates: readonly Candidate[],
  points: readonly EdgePoint[],
  baseline: {readonly x: number; readonly y: number} | null,
  chain: ReadonlySet<string>,
  lit: ReadonlySet<string>,
): ChartEdgeView[] {
  const byId = new Map(candidates.map(item => [item.id, item]));
  const passed = new Set(points.filter(point => point.passed).map(point => point.id));
  const edges: ChartEdgeView[] = [];
  for (const point of points) {
    const item = byId.get(point.id);
    if (!point.passed || item === undefined) continue;
    const base = baseWithPoint(item, byId, passed);
    if (base === null && baseline === null) continue;
    const from: EdgeFrom = base ?? (baseline as {x: number; y: number});
    const along = (set: ReadonlySet<string>): boolean =>
      set.has(point.id) && (base === null || set.has(base));
    const emphasis = along(lit) ? 'lit' : along(chain) ? 'strong' : undefined;
    edges.push({
      from,
      to: point.id,
      ...(emphasis === undefined ? {} : {emphasis}),
      faint: point.faint,
    });
  }
  return edges;
}
