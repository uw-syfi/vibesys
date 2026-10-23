import {describe, expect, test} from 'bun:test';
import {candidateLineage} from './index.js';
import type {Candidate, CandidateOutcome, CandidateTree} from './types.js';

/** A small seeded generator (mulberry32): the same seed gives the same tree. */
function randoms(seed: number): () => number {
  let state = seed >>> 0 || 1;
  return () => {
    state = (state + 0x6d2b79f5) >>> 0;
    let t = state;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function candidate(
  index: number,
  parent: number | null,
  outcome: CandidateOutcome,
  ratio: number | null,
): Candidate {
  return {
    id: `c${index}`,
    hypothesis: null,
    parent: parent === null ? null : `c${parent}`,
    title: `idea ${index}`,
    idea: null,
    round: null,
    outcome,
    reason: null,
    baseRound: null,
    measurement:
      ratio === null
        ? null
        : {
            span: `g${index}`,
            at: index,
            metricValue: ratio,
            vsBaseline: ratio,
            against: null,
            readings: [],
            inputReadings: [],
            verdict: null,
            failed: null,
          },
    attempts: [],
    startedAt: index,
    endedAt: null,
    running: [],
  };
}

interface Generated {
  readonly tree: CandidateTree;
  readonly parents: readonly (number | null)[];
}

function generated(seed: number): Generated {
  const next = randoms(seed);
  const count = 1 + Math.floor(next() * 60);
  const parents: (number | null)[] = [];
  const items: Candidate[] = [];
  for (let index = 0; index < count; index += 1) {
    const roll = next();
    const parent =
      index === 0 || roll < 0.1 ? null : roll < 0.6 ? index - 1 : Math.floor(next() * index);
    parents.push(parent);
    const outcome = (['passed', 'failed', 'running'] as const)[Math.floor(next() * 3)] ?? 'passed';
    items.push(candidate(index, parent, outcome, next() < 0.7 ? 0.5 + next() * 3 : null));
  }
  return {tree: {candidates: items, best: null, bestPath: [], adoptionOpened: false}, parents};
}

const SEEDS = Array.from({length: 200}, (_, at) => at + 1);

describe('candidateLineage', () => {
  test('every idea is a root or the child of its base, exactly once', () => {
    for (const seed of SEEDS) {
      const {tree, parents} = generated(seed);
      const lineage = candidateLineage(tree);
      const placed: string[] = [...lineage.roots];
      for (const kids of lineage.children.values()) placed.push(...kids);
      expect(placed.sort()).toEqual(tree.candidates.map(item => item.id).sort());
      parents.forEach((parent, index) => {
        const id = `c${index}`;
        if (parent === null) expect(lineage.roots).toContain(id);
        else expect(lineage.children.get(`c${parent}`)).toContain(id);
      });
    }
  });

  test('children and roots keep the order the ideas began in', () => {
    for (const seed of SEEDS) {
      const lineage = candidateLineage(generated(seed).tree);
      const index = (id: string): number => Number(id.slice(1));
      for (const ids of [lineage.roots, ...lineage.children.values()]) {
        expect(ids.map(index)).toEqual([...ids.map(index)].sort((a, b) => a - b));
      }
    }
  });

  test('a subtree counts the idea and the counts of its children, by outcome', () => {
    for (const seed of SEEDS) {
      const {tree} = generated(seed);
      const lineage = candidateLineage(tree);
      for (const item of tree.candidates) {
        const stats = lineage.subtree.get(item.id);
        if (stats === undefined) throw new Error(`no stats for ${item.id}`);
        const kids = (lineage.children.get(item.id) ?? []).map(id => lineage.subtree.get(id));
        const sum = (pick: (each: NonNullable<(typeof kids)[number]>) => number): number =>
          kids.reduce((total, each) => total + (each === undefined ? 0 : pick(each)), 0);
        expect(stats.size).toBe(1 + sum(each => each.size));
        expect(stats.passed + stats.failed + stats.running).toBe(stats.size);
        expect(stats.failed).toBe((item.outcome === 'failed' ? 1 : 0) + sum(each => each.failed));
      }
      const roots = lineage.roots.reduce(
        (total, id) => total + (lineage.subtree.get(id)?.size ?? 0),
        0,
      );
      expect(roots).toBe(tree.candidates.length);
    }
  });

  test('depth counts the steps from the input, and a base the tree lacks reads as the input', () => {
    const {tree, parents} = generated(7);
    const lineage = candidateLineage(tree);
    parents.forEach((parent, index) => {
      const want = parent === null ? 1 : (lineage.depth.get(`c${parent}`) ?? 0) + 1;
      expect(lineage.depth.get(`c${index}`)).toBe(want);
    });
    const orphan = candidate(0, null, 'passed', 1);
    const lost = {...orphan, parent: 'gone'};
    const only = candidateLineage({
      candidates: [lost],
      best: null,
      bestPath: [],
      adoptionOpened: false,
    });
    expect(only.roots).toEqual(['c0']);
  });

  test('is the same object while the tree is unchanged', () => {
    const {tree} = generated(3);
    expect(candidateLineage(tree)).toBe(candidateLineage(tree));
  });
});
