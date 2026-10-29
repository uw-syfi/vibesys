import {describe, expect, test} from 'bun:test';
import {
  type Direction,
  type Directions,
  dominates,
  frontierAt,
  paretoFrontier,
  type Tradeoff,
} from './index.js';

/** A small seeded generator, so a failing case repeats. */
function random(seed: number): () => number {
  let state = seed;
  return () => {
    state = (state * 1664525 + 1013904223) % 4294967296;
    return state / 4294967296;
  };
}

interface Named extends Tradeoff {
  readonly id: number;
}

const DIRECTIONS: readonly Directions[] = (['higher', 'lower'] as const).flatMap(x =>
  (['higher', 'lower'] as const).map((y): Directions => ({x, y})),
);

/** Points on a coarse grid, so ties and duplicates are common, in a random order. */
function pointsOf(next: () => number): Named[] {
  const count = Math.floor(next() * 25);
  return Array.from({length: count}, (_, id) => ({
    id,
    x: Math.floor(next() * 8),
    y: Math.floor(next() * 8),
    time: Math.floor(next() * 10),
  }));
}

function eachCase(check: (points: Named[], directions: Directions, next: () => number) => void) {
  const next = random(11);
  for (let round = 0; round < 400; round += 1) {
    const directions = DIRECTIONS[Math.floor(next() * DIRECTIONS.length)] as Directions;
    check(pointsOf(next), directions, next);
  }
}

const idsOf = (list: readonly Named[]): number[] => list.map(p => p.id).sort((a, b) => a - b);

describe('paretoFrontier', () => {
  test('its points are mutually non-dominated', () => {
    eachCase((points, directions) => {
      const frontier = paretoFrontier(points, directions);
      for (const a of frontier) {
        for (const b of frontier) expect(dominates(a, b, directions)).toBe(false);
      }
    });
  });

  test('every point is on the frontier or dominated by a point of it', () => {
    eachCase((points, directions) => {
      const frontier = paretoFrontier(points, directions);
      const ids = new Set(frontier.map(p => p.id));
      for (const point of points) {
        const covered = ids.has(point.id) || frontier.some(f => dominates(f, point, directions));
        expect(covered).toBe(true);
      }
    });
  });

  test('it keeps exactly the points no other point dominates', () => {
    eachCase((points, directions) => {
      const expected = idsOf(points.filter(p => !points.some(q => dominates(q, p, directions))));
      expect(idsOf(paretoFrontier(points, directions))).toEqual(expected);
    });
  });

  test('the input order does not change the result', () => {
    eachCase((points, directions, next) => {
      const shuffled = [...points].sort(() => next() - 0.5);
      expect(idsOf(paretoFrontier(shuffled, directions))).toEqual(
        idsOf(paretoFrontier(points, directions)),
      );
    });
  });

  test('negating an axis and flipping its direction changes nothing', () => {
    const flip = (d: Direction): Direction => (d === 'higher' ? 'lower' : 'higher');
    eachCase((points, directions) => {
      const mirrored = points.map(p => ({...p, x: -p.x}));
      expect(idsOf(paretoFrontier(mirrored, {...directions, x: flip(directions.x)}))).toEqual(
        idsOf(paretoFrontier(points, directions)),
      );
    });
  });

  test('a frontier is ordered from the best x, so y gets worse along it', () => {
    eachCase((points, directions) => {
      const frontier = paretoFrontier(points, directions);
      const sign = (d: Direction): number => (d === 'higher' ? 1 : -1);
      for (let i = 1; i < frontier.length; i += 1) {
        const a = frontier[i - 1] as Named;
        const b = frontier[i] as Named;
        expect(sign(directions.x) * (a.x - b.x)).toBeGreaterThanOrEqual(0);
        expect(sign(directions.y) * (a.y - b.y)).toBeLessThanOrEqual(0);
      }
    });
  });

  test('equal points are all kept, and an empty set has an empty frontier', () => {
    const same = [
      {id: 1, x: 2, y: 3, time: 0},
      {id: 2, x: 2, y: 3, time: 1},
    ];
    expect(paretoFrontier(same, {x: 'higher', y: 'higher'})).toHaveLength(2);
    expect(paretoFrontier([], {x: 'higher', y: 'lower'})).toEqual([]);
  });
});

describe('frontierAt', () => {
  test('is the frontier of the points measured by then, and what it drops a later point dominates', () => {
    eachCase((points, directions, next) => {
      const time = Math.floor(next() * 10);
      const frontier = frontierAt(points, directions, time);
      const upTo = points.filter(p => p.time <= time);
      expect(frontier).toEqual(paretoFrontier(upTo, directions));
      // As the run goes on, a point leaves the frontier only when another point dominates it.
      const later = frontierAt(points, directions, time + 1);
      for (const point of frontier) {
        const kept = later.some(p => p.id === (point as Named).id);
        expect(kept || later.some(p => dominates(p, point, directions))).toBe(true);
      }
    });
  });
});
