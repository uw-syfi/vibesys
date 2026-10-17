/** Which way along an axis is better. */
export type Direction = 'higher' | 'lower';

/** The directions of the two axes of a trade-off. */
export interface Directions {
  readonly x: Direction;
  readonly y: Direction;
}

/** A point of a two-objective trade-off; `time` is when it was measured, in any one unit. */
export interface Tradeoff {
  readonly x: number;
  readonly y: number;
  readonly time: number;
}

/** A value turned so that larger is always better, whatever the axis' direction. */
export function toward(value: number, direction: Direction): number {
  return direction === 'higher' ? value : -value;
}

/**
 * Whether `a` dominates `b`: at least as good on both axes and strictly better on one. A point
 * never dominates itself or a point with the same two values.
 */
export function dominates(a: Tradeoff, b: Tradeoff, directions: Directions): boolean {
  const ax = toward(a.x, directions.x);
  const bx = toward(b.x, directions.x);
  const ay = toward(a.y, directions.y);
  const by = toward(b.y, directions.y);
  return ax >= bx && ay >= by && (ax > bx || ay > by);
}

/**
 * The Pareto frontier of `points`: those no other point dominates, ordered from the best on x to
 * the best on y (each step toward better x is a step toward worse y). Points with equal values
 * are all kept, since neither dominates the other. The order of `points` does not matter, and
 * the result holds the same objects.
 */
export function paretoFrontier<P extends Tradeoff>(
  points: readonly P[],
  directions: Directions,
): readonly P[] {
  // Sweep from the best x down. A point survives when it has the best y at its own x and beats
  // every y seen at a strictly better x (equal y at a worse x is dominated).
  const ranked = points
    .map(point => ({
      point,
      x: toward(point.x, directions.x),
      y: toward(point.y, directions.y),
    }))
    .sort((a, b) => b.x - a.x || b.y - a.y);
  const kept: P[] = [];
  let betterY = Number.NEGATIVE_INFINITY;
  let groupX = Number.NaN;
  let groupBest = Number.NEGATIVE_INFINITY;
  for (const {point, x, y} of ranked) {
    if (x !== groupX) {
      betterY = Math.max(betterY, groupBest);
      groupX = x;
      groupBest = y;
    }
    if (y === groupBest && y > betterY) kept.push(point);
  }
  return kept;
}

/** The frontier of the points measured by `time` (inclusive): the trade-off as it stood then. */
export function frontierAt<P extends Tradeoff>(
  points: readonly P[],
  directions: Directions,
  time: number,
): readonly P[] {
  return paretoFrontier(
    points.filter(point => point.time <= time),
    directions,
  );
}
