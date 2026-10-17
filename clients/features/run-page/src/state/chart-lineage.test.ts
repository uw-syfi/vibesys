import {describe, expect, test} from 'bun:test';
import type {CandidateOutcome} from '@vibesys/run-state/candidates';
import {Seeded} from '@vibesys/store/testing';
import {chartEdges, type EdgePoint} from './chart-lineage.js';
import {ideaAncestry} from './ideas-tree.js';
import {idOf, modelOfShape, type Shape} from './ideas-tree-fixtures.js';
import {stripChartView} from './run-level.js';

const SEEDS = Array.from({length: 120}, (_, at) => at + 1);
const BASELINE = {x: 0, y: 100};
const NONE: ReadonlySet<string> = new Set();

function shaped(seed: number): Shape {
  const random = new Seeded(seed);
  const count = 1 + random.int(40);
  const parents: (number | null)[] = [];
  const outcomes: CandidateOutcome[] = [];
  for (let index = 0; index < count; index += 1) {
    const roll = random.next();
    parents.push(index === 0 || roll < 0.1 ? null : roll < 0.6 ? index - 1 : random.int(index));
    outcomes.push(random.pick<CandidateOutcome>(['passed', 'passed', 'failed', 'running']));
  }
  const passed = outcomes.flatMap((outcome, at) => (outcome === 'passed' ? [at] : []));
  return passed.length === 0 ? {parents, outcomes} : {parents, outcomes, best: random.pick(passed)};
}

/** The points the chart draws for `plan`: every idea but the running ones is measured. */
function pointsOf(plan: Shape): EdgePoint[] {
  return plan.parents.flatMap((_, at) => {
    const outcome = plan.outcomes?.[at] ?? 'passed';
    return outcome === 'running'
      ? []
      : [{id: idOf(at), passed: outcome === 'passed', faint: at % 7 === 0}];
  });
}

function edgesOf(plan: Shape, lit: ReadonlySet<string> = NONE) {
  const model = modelOfShape(plan);
  return {
    model,
    edges: chartEdges(
      model.candidates.candidates,
      pointsOf(plan),
      BASELINE,
      new Set(model.candidates.bestPath),
      lit,
    ),
  };
}

type Edge = ReturnType<typeof chartEdges>[number];

/** Whether `edge` runs along `set`: its point is in it, and so is its base unless that is the baseline. */
function along(edge: Edge, set: ReadonlySet<string>): boolean {
  return set.has(edge.to) && (typeof edge.from !== 'string' || set.has(edge.from));
}

/** The base of idea `id` as the chart connects it: the nearest ancestor that passed, else the baseline. */
function expectedFrom(plan: Shape, passed: ReadonlySet<string>, id: string) {
  let base = plan.parents[Number(id.slice(1))] ?? null;
  while (base !== null && !passed.has(idOf(base))) base = plan.parents[base] ?? null;
  return base === null ? BASELINE : idOf(base);
}

describe('the chart’s connectors', () => {
  test('each passed idea has one connector to its base or the nearest ancestor that passed', () => {
    for (const seed of SEEDS) {
      const plan = shaped(seed);
      const passed = new Set(pointsOf(plan).flatMap(p => (p.passed ? [p.id] : [])));
      const {edges} = edgesOf(plan);
      expect(edges.map(edge => edge.to).sort()).toEqual([...passed].sort());
      for (const edge of edges) expect(edge.from).toEqual(expectedFrom(plan, passed, edge.to));
    }
  });

  test('a failed or running idea has no connector, and no connector leaves one', () => {
    for (const seed of SEEDS) {
      const plan = shaped(seed);
      const {edges} = edgesOf(plan);
      const out = new Set(
        plan.parents.flatMap((_, at) => (plan.outcomes?.[at] === 'passed' ? [] : [idOf(at)])),
      );
      for (const edge of edges) {
        expect(out.has(edge.to)).toBe(false);
        if (typeof edge.from === 'string') expect(out.has(edge.from)).toBe(false);
      }
    }
  });

  test('following the connectors from any point reaches the baseline', () => {
    for (const seed of SEEDS) {
      const {edges} = edgesOf(shaped(seed));
      const from = new Map(edges.map(edge => [edge.to, edge.from]));
      for (const edge of edges) {
        let at: string | {x: number; y: number} | undefined = edge.to;
        for (let steps = 0; typeof at === 'string'; steps += 1) {
          expect(steps).toBeLessThanOrEqual(edges.length);
          at = from.get(at);
        }
        expect(at).toEqual(BASELINE);
      }
    }
  });

  test('the best chain is strong and the chain of the lit idea is lit, each a path from the baseline', () => {
    for (const seed of SEEDS) {
      const plan = shaped(seed);
      const base = edgesOf(plan);
      const chain = new Set(base.model.candidates.bestPath);
      const focus = idOf(new Seeded(seed).int(plan.parents.length));
      const lit = ideaAncestry(base.model, focus);
      for (const edge of edgesOf(plan, lit).edges) {
        const emphasis = along(edge, lit) ? 'lit' : along(edge, chain) ? 'strong' : undefined;
        expect(edge.emphasis).toBe(emphasis);
      }
    }
  });

  test('without a baseline the ideas built on the input have no connector, and the rest still do', () => {
    const plan: Shape = {parents: [null, 0, 1]};
    const model = modelOfShape(plan);
    const edges = chartEdges(model.candidates.candidates, pointsOf(plan), null, NONE, NONE);
    expect(edges.map(edge => [edge.from, edge.to])).toEqual([
      [idOf(0), idOf(1)],
      [idOf(1), idOf(2)],
    ]);
  });

  test('a connector is faint with its point', () => {
    const plan: Shape = {parents: [null, 0, 1, 2, 3, 4, 5, 6, 7]};
    const {edges} = edgesOf(plan);
    expect(edges.map(edge => edge.faint)).toEqual(pointsOf(plan).map(point => point.faint));
  });

  test('the strip chart carries them and moves nothing when one is lit', () => {
    for (const seed of SEEDS.slice(0, 40)) {
      const plan = shaped(seed);
      const model = modelOfShape(plan);
      const plain = stripChartView(model, 0, null);
      if (plain === null) continue;
      const lit = stripChartView(model, 0, null, null, null, ideaAncestry(model, idOf(0)));
      expect(lit?.points).toEqual(plain.points);
      expect(lit?.yDomain).toEqual(plain.yDomain);
      expect(lit?.steps).toEqual(plain.steps);
      expect(lit?.xTicks).toEqual(plain.xTicks);
      expect(plain.edges.length).toBe(plan.outcomes?.filter(o => o === 'passed').length ?? 0);
    }
  });
});
