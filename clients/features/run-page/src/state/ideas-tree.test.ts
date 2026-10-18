import {describe, expect, test} from 'bun:test';
import type {CandidateOutcome} from '@vibesys/run-state/candidates';
import {Seeded} from '@vibesys/store/testing';
import {
  bestLine,
  CHAIN_TAIL,
  COMPRESS_FROM,
  defaultLayout,
  EARLIER,
  type IdeaTree,
  INDENT_CAP,
  ideaAncestry,
  ideaTree,
  type TreeShape,
} from './ideas-tree.js';
import {idOf, modelOfShape, type Shape} from './ideas-tree-fixtures.js';
import {IDEAS_FILTER_DEFAULT} from './ideas-view.js';
import type {RunModel} from './run-model.js';

const ALL = {...IDEAS_FILTER_DEFAULT, failed: true};
const SEEDS = Array.from({length: 150}, (_, at) => at + 1);

function shaped(seed: number, depthy = false): Shape {
  const random = new Seeded(seed);
  const count = 1 + random.int(45);
  const parents: (number | null)[] = [];
  const outcomes: CandidateOutcome[] = [];
  for (let index = 0; index < count; index += 1) {
    const roll = random.next();
    parents.push(
      index === 0 || roll < 0.08
        ? null
        : roll < (depthy ? 0.85 : 0.5)
          ? index - 1
          : random.int(index),
    );
    outcomes.push(random.pick<CandidateOutcome>(['passed', 'passed', 'failed', 'running']));
  }
  const passed = outcomes.flatMap((outcome, index) => (outcome === 'passed' ? [index] : []));
  return passed.length === 0 ? {parents, outcomes} : {parents, outcomes, best: random.pick(passed)};
}

function shape(filter = ALL, open: ReadonlySet<string> = new Set(), pinned: string | null = null) {
  return {filter, open, pinned} satisfies TreeShape;
}

/** Opens every folded row until the tree shows each idea. */
function expandedAll(model: RunModel, filter = ALL, pinned: string | null = null): IdeaTree {
  const open = new Set<string>();
  for (;;) {
    const tree = ideaTree(model, shape(filter, open, pinned));
    const before = open.size;
    for (const entry of tree.rows) {
      if (entry.kind === 'fold') open.add(entry.key);
      else if (entry.below === 'closed') open.add(entry.row.id);
    }
    if (open.size === before) return tree;
  }
}

/** A row's level from the root: its guides, or its written depth once they are capped. */
const levelOf = (entry: {guides: readonly unknown[]; depth: number | null}): number =>
  entry.depth === null ? entry.guides.length : entry.depth - 1;

const numberOf = (id: string): number => Number(id.slice(1));

/** The ideas below `index` (at any depth) that `keep` accepts. */
function descendants(plan: Shape, index: number, keep: (each: number) => boolean): number {
  return plan.parents.reduce<number>(
    (total, parent, each) =>
      parent === index ? total + (keep(each) ? 1 : 0) + descendants(plan, each, keep) : total,
    0,
  );
}

function expectSummary(plan: Shape, index: number, words: string): void {
  const count = descendants(plan, index, () => true);
  const lost = descendants(plan, index, each => plan.outcomes?.[each] === 'failed');
  expect(words.startsWith(`→ ${count} ${count === 1 ? 'idea' : 'ideas'}`)).toBe(true);
  expect(words.includes('failed')).toBe(lost > 0);
  if (lost > 0) expect(words).toContain(`${lost} failed`);
}

function ids(tree: IdeaTree): string[] {
  return tree.rows.flatMap(entry => (entry.kind === 'idea' ? [entry.row.id] : []));
}

describe('the ideas tree', () => {
  test('expanding everything shows each idea exactly once, under its base', () => {
    for (const seed of SEEDS) {
      const plan = shaped(seed);
      const model = modelOfShape(plan);
      const tree = expandedAll(model);
      expect(ids(tree).sort()).toEqual(plan.parents.map((_, at) => idOf(at)).sort());
      expect(tree.rows.every(entry => entry.kind === 'idea')).toBe(true);
      // Row by row: a node's parent is the nearest row above it one level up (none for a root).
      const placed = tree.rows.flatMap(entry => (entry.kind === 'idea' ? [entry] : []));
      placed.forEach((entry, at) => {
        const level = levelOf(entry);
        const index = Number(entry.row.id.slice(1));
        const base = plan.parents[index] ?? null;
        const above = placed
          .slice(0, at)
          .reverse()
          .find(each => levelOf(each) === level - 1);
        expect(level === 0 ? null : (above?.row.id ?? null)).toBe(
          base === null ? null : idOf(base),
        );
      });
    }
  });

  test('a closed idea says what is below it', () => {
    for (const seed of SEEDS) {
      const plan = shaped(seed);
      const tree = ideaTree(modelOfShape(plan), shape());
      for (const entry of tree.rows) {
        if (entry.kind === 'idea' && entry.below === 'closed') {
          expectSummary(plan, numberOf(entry.row.id), entry.summary ?? '');
        }
      }
    }
  });

  test('children come newest first', () => {
    for (const seed of SEEDS) {
      const plan = shaped(seed);
      const placed = expandedAll(modelOfShape(plan)).rows.flatMap(entry =>
        entry.kind === 'idea' ? [entry] : [],
      );
      for (const entry of placed) {
        const level = entry.guides.length;
        if (level >= INDENT_CAP) continue;
        const base = plan.parents[numberOf(entry.row.id)];
        const siblings = placed.filter(
          each => each.guides.length === level && plan.parents[numberOf(each.row.id)] === base,
        );
        const order = siblings.map(each => numberOf(each.row.id));
        expect(order).toEqual([...order].sort((a, b) => b - a));
      }
    }
  });

  test('the best chain is always open and marked; the rest start closed', () => {
    for (const seed of SEEDS) {
      const plan = shaped(seed, true);
      const model = modelOfShape(plan);
      const tree = ideaTree(model, shape());
      const chain = model.candidates.bestPath;
      const shown = new Set(ids(tree));
      const hidden = chain.length - (CHAIN_TAIL + 1);
      const hides = hidden >= 2;
      chain.forEach((id, at) => {
        if (hides && at < hidden) {
          expect(shown.has(id)).toBe(false);
          return;
        }
        expect(shown.has(id)).toBe(true);
        const entry = tree.rows.find(each => each.kind === 'idea' && each.row.id === id);
        if (entry?.kind !== 'idea') throw new Error('chain row missing');
        expect(entry.row.stripe).toBe(true);
        expect(['none', 'held']).toContain(entry.below);
      });
      expect(tree.rows.some(entry => entry.kind === 'fold' && entry.key === EARLIER)).toBe(hides);
      for (const entry of tree.rows) {
        if (entry.kind === 'idea' && !chain.includes(entry.row.id)) {
          expect(entry.below === 'closed' || entry.below === 'none' || entry.below === 'open').toBe(
            true,
          );
          expect(entry.row.stripe).toBe(false);
        }
      }
    }
  });

  test('a long one-child run folds into one row that expands to every idea', () => {
    // 0 on the input; 1..9 each on the one before; 10 and 11 both on 9: an open 0 folds 1..8.
    const parents: (number | null)[] = [null, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 9];
    const model = modelOfShape({parents});
    const folded = ideaTree(model, shape(ALL, new Set([idOf(0)])));
    const fold = folded.rows.find(entry => entry.kind === 'fold');
    expect(fold?.kind === 'fold' ? fold.label : null).toBe('… 8 ideas …');
    expect(ids(folded)).toEqual([0, 9, 11, 10].map(idOf));
    const key = fold?.kind === 'fold' ? fold.key : '';
    const opened = ideaTree(model, shape(ALL, new Set([idOf(0), key])));
    expect(opened.rows.some(entry => entry.kind === 'fold')).toBe(false);
    expect(ids(opened).sort()).toEqual(parents.map((_, at) => idOf(at)).sort());
    // Fewer than COMPRESS_FROM in between stay as rows.
    const short = modelOfShape({parents: [null, 0, 1, 2, 2]});
    const rows = ideaTree(short, shape(ALL, new Set([idOf(0)])));
    expect(rows.rows.some(entry => entry.kind === 'fold')).toBe(false);
    expect(COMPRESS_FROM).toBeGreaterThan(2);
  });

  test('guides stop at the cap and the lineage depth is written instead', () => {
    const parents: (number | null)[] = Array.from({length: 14}, (_, at) =>
      at === 0 ? null : at - 1,
    );
    const model = modelOfShape({parents, best: 13});
    const tree = ideaTree(model, shape(ALL, new Set([EARLIER])));
    for (const entry of tree.rows) {
      if (entry.kind !== 'idea') continue;
      expect(entry.guides.length).toBeLessThanOrEqual(INDENT_CAP);
      const index = Number(entry.row.id.slice(1));
      expect(entry.depth).toBe(index > INDENT_CAP ? index + 1 : null);
    }
  });

  test('the best chain’s earlier ancestors fold into one row until opened', () => {
    const parents: (number | null)[] = Array.from({length: 14}, (_, at) =>
      at === 0 ? null : at - 1,
    );
    const model = modelOfShape({parents, best: 13});
    const folded = ideaTree(model, shape());
    expect(folded.rows[0]).toMatchObject({
      kind: 'fold',
      label: `… ${14 - (CHAIN_TAIL + 1)} earlier`,
    });
    expect(ids(folded)).toEqual(Array.from({length: CHAIN_TAIL + 1}, (_, at) => idOf(8 + at)));
    expect(ids(ideaTree(model, shape(ALL, new Set([EARLIER]))))).toHaveLength(14);
  });

  test('pinning the best keeps the older ancestors folded; pinning one of them opens them', () => {
    const parents: (number | null)[] = Array.from({length: 14}, (_, at) =>
      at === 0 ? null : at - 1,
    );
    const model = modelOfShape({parents, best: 13});
    const pinnedBest = ideaTree(model, shape(ALL, new Set(), idOf(13)));
    expect(pinnedBest.rows[0]).toMatchObject({kind: 'fold', key: EARLIER});
    expect(ids(pinnedBest)).toHaveLength(CHAIN_TAIL + 1);
    // A side branch off an early ancestor must be reachable.
    const branched = modelOfShape({parents: [...parents, 2], best: 13});
    expect(ids(ideaTree(branched, shape(ALL, new Set(), idOf(14))))).toContain(idOf(14));
  });

  test('a row says nothing under its title when its words only repeat it', () => {
    const model = modelOfShape({parents: [null, null]});
    const items = model.candidates.candidates;
    const worded = items.map((item, at) => ({
      ...item,
      idea: at === 0 ? 'Try: IDEA 0.' : 'Try: something else',
    }));
    const again = {...model, candidates: {...model.candidates, candidates: worded}} as RunModel;
    const rows = ideaTree(again, shape()).rows.flatMap(entry =>
      entry.kind === 'idea' ? [entry.row] : [],
    );
    expect(rows.map(row => row.summary)).toEqual(['Try: something else', null]);
  });

  test('a search shows the ideas that match and the ideas they build on, all open', () => {
    for (const seed of SEEDS) {
      const plan = shaped(seed);
      const model = modelOfShape(plan);
      const wanted = new Set<string>();
      const random = new Seeded(seed * 7);
      const match = random.int(plan.parents.length);
      const tree = ideaTree(model, shape({...ALL, query: `idea ${match}`}));
      // `idea 1` also matches `idea 10`..`idea 19`; compute the matches the way the text does.
      const matching = plan.parents.flatMap((_, at) =>
        `idea ${at}`.includes(`idea ${match}`) ? [at] : [],
      );
      for (const at of matching) {
        for (const id of ideaAncestry(model, idOf(at))) wanted.add(id);
      }
      expect(new Set(ids(tree))).toEqual(wanted);
      expect(ids(tree)).toHaveLength(wanted.size);
      expect(tree.rows.every(entry => entry.kind === 'idea' && entry.below !== 'closed')).toBe(
        true,
      );
    }
  });

  test('a pinned idea has its ancestors open', () => {
    const parents: (number | null)[] = [null, 0, 0, 1, 1, 2, 3, 3, 6];
    const model = modelOfShape({parents});
    const tree = ideaTree(model, shape(ALL, new Set(), idOf(8)));
    expect(ids(tree)).toContain(idOf(8));
    expect(new Set(ideaAncestry(model, idOf(8)))).toEqual(new Set([8, 6, 3, 1, 0].map(idOf)));
  });

  test('the layout starts as a tree once an idea builds on another', () => {
    expect(defaultLayout(modelOfShape({parents: [null, null, null]}))).toBe('list');
    expect(defaultLayout(modelOfShape({parents: [null, 0]}))).toBe('tree');
  });

  test('the best line names the best idea and its steps from the input', () => {
    expect(bestLine(modelOfShape({parents: [null, 0, 1, 1], best: 2}))).toEqual({
      id: idOf(2),
      title: 'idea 2',
      steps: 3,
    });
    expect(bestLine(modelOfShape({parents: [null, 0]}))).toBeNull();
  });

  test('the same run and shape builds the same rows', () => {
    const model = modelOfShape(shaped(5));
    expect(ideaTree(model, shape()).rows).toEqual(ideaTree(model, shape()).rows);
  });
});

describe('failed ideas', () => {
  test('are in the tree by default, each under its base like any other idea', () => {
    for (const seed of SEEDS) {
      const plan = shaped(seed);
      const model = modelOfShape(plan);
      const failed = model.candidates.candidates.filter(item => item.outcome === 'failed');
      const tree = expandedAll(model, IDEAS_FILTER_DEFAULT);
      const shown = new Set(
        tree.rows.flatMap(entry => (entry.kind === 'idea' ? [entry.row.id] : [])),
      );
      for (const item of failed) expect(shown.has(item.id), `seed ${seed}`).toBe(true);
      expect(tree.hiddenFailed).toBe(0);
    }
  });

  test('can be hidden by the filter, and the folded summaries still count them', () => {
    for (const seed of SEEDS) {
      const model = modelOfShape(shaped(seed));
      const hidden = ideaTree(model, shape({...IDEAS_FILTER_DEFAULT, failed: false}));
      const failed = model.candidates.candidates.filter(item => item.outcome === 'failed');
      expect(hidden.hiddenFailed).toBe(failed.length);
      expect(hidden.counts.failed).toBe(failed.length);
    }
  });
});

describe('a deep row', () => {
  test('keeps one dotted cell for the levels it leaves out, and its true depth', () => {
    const deep = modelOfShape({
      parents: Array.from({length: INDENT_CAP + 4}, (_, at) => (at === 0 ? null : at - 1)),
      outcomes: Array.from({length: INDENT_CAP + 4}, () => 'passed' as CandidateOutcome),
    });
    const tree = expandedAll(deep, ALL);
    const capped = tree.rows.filter(entry => entry.kind === 'idea' && entry.depth !== null);
    expect(capped.length).toBeGreaterThan(0);
    for (const entry of capped) {
      expect(entry.guides[0]).toBe('gap');
      expect(entry.kind === 'idea' ? (entry.depth ?? 0) : 0).toBeGreaterThan(INDENT_CAP);
    }
  });
});
