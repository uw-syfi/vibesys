import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {IdeaVerdict, PlanStatus, SearchStateSchema} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {
  type IdeaRow,
  type IdeaSortKey,
  ideasInRun,
  ideasInWindow,
  ideasMatching,
  ideasOfVerdict,
  ideasReport,
  nextSort,
  type SortDirection,
  sortIdeas,
  type Verdict,
  verdictCounts,
} from './ideas.js';

const verdict = fc.constantFrom<Verdict>('open', 'kept', 'rejected', 'parked');
const when = fc.option(fc.integer({min: 0, max: 1000}), {nil: null});

const row = fc
  .record({
    planNumber: fc.integer({min: 1, max: 500}),
    verdict,
    createdMs: when,
    updatedMs: when,
    inRun: fc.boolean(),
  })
  .map(
    (fields): IdeaRow => ({
      title: 'idea',
      workstream: 0,
      workstreamTitle: null,
      gains: [],
      candidateJobIds: [],
      agent: '',
      ...fields,
    }),
  );
const rows = fc.array(row, {maxLength: 25});
const window = fc.array(fc.integer({min: 1, max: 500}), {maxLength: 30}).map(plans => ({plans}));

describe('the display filters of the ideas table', () => {
  it('"check-in" keeps exactly the ideas whose plan the host counted for the window, in order', () => {
    fc.assert(
      fc.property(rows, window, (all, span) => {
        const kept = ideasInWindow(all, span);
        expect(kept).toEqual(all.filter(idea => span.plans.includes(idea.planNumber)));
      }),
    );
  });

  it('a window that counts more plans never shows fewer ideas, and one that counts none shows none', () => {
    fc.assert(
      fc.property(rows, window, window, (all, span, more) => {
        const wider = {plans: [...span.plans, ...more.plans]};
        const narrow = new Set(ideasInWindow(all, span));
        const wide = new Set(ideasInWindow(all, wider));
        for (const idea of narrow) expect(wide.has(idea)).toBe(true);
        expect(ideasInWindow(all, {plans: []})).toEqual([]);
      }),
    );
  });

  it('filters by verdict and by run are subsets that change nothing about a row and filtering twice changes nothing', () => {
    fc.assert(
      fc.property(rows, verdict, (all, which) => {
        const byVerdict = ideasOfVerdict(all, which);
        expect(byVerdict.every(idea => idea.verdict === which)).toBe(true);
        expect(ideasOfVerdict(byVerdict, which)).toEqual(byVerdict);
        const mine = ideasInRun(all);
        expect(mine.every(idea => idea.inRun)).toBe(true);
        expect(mine.length + all.filter(idea => !idea.inRun).length).toBe(all.length);
        for (const idea of byVerdict) expect(all).toContain(idea);
      }),
    );
  });
});

describe('the ideas of a search state', () => {
  it('are none before a search state is served', () => {
    expect(ideasReport(undefined)).toBeUndefined();
  });

  it('join each plan to its served view, newest first, and copy the served gains and verdicts as they are', () => {
    fc.assert(
      fc.property(
        fc.uniqueArray(fc.integer({min: 1, max: 60}), {maxLength: 20}),
        fc.double({min: 0.1, max: 9, noNaN: true}),
        (numbers, ratio) => {
          const search = create(SearchStateSchema, {
            plans: numbers.map(number => ({
              number,
              title: `idea ${number}`,
              status: PlanStatus.WON,
              createdAt: timestampFromMs(number * 1000),
              workstream: number % 3,
              origin: {runId: 'r', agent: 'impl-1', at: timestampFromMs(number)},
            })),
            ideas: numbers.map(number => ({
              planNumber: number,
              verdict: IdeaVerdict.KEPT,
              candidateJobIds: [`job-${number}`],
              best: [{metric: 'tok_s', ratio, bestValue: 2 * ratio, baselineValue: 2}],
              inRun: number % 2 === 0,
            })),
            workstreams: [{number: 1, title: 'Decode path', status: 1}],
            unplanned: 3,
            truncated: true,
          });
          const report = ideasReport(search);
          expect(report?.ideas.map(idea => idea.planNumber)).toEqual(
            [...numbers].sort((a, b) => b - a),
          );
          for (const idea of report?.ideas ?? []) {
            expect(idea.verdict).toBe('kept');
            expect(idea.gains).toEqual([
              {metric: 'tok_s', ratio, bestValue: 2 * ratio, baselineValue: 2},
            ]);
            expect(idea.candidateJobIds).toEqual([`job-${idea.planNumber}`]);
            expect(idea.inRun).toBe(idea.planNumber % 2 === 0);
            expect(idea.workstreamTitle).toBe(idea.workstream === 1 ? 'Decode path' : null);
          }
          expect([report?.unplanned, report?.truncated]).toEqual([3, true]);
        },
      ),
    );
  });

  it('takes a plan the host sent no view for as open and not in the run, not as a guess', () => {
    const report = ideasReport(
      create(SearchStateSchema, {plans: [{number: 4, title: 'x', status: PlanStatus.WON}]}),
    );
    expect(report?.ideas[0]).toMatchObject({verdict: 'open', inRun: false, gains: []});
  });
});

const sortKey = fc.constantFrom<IdeaSortKey>(
  'id',
  'idea',
  'workstream',
  'verdict',
  'gain',
  'agent',
  'updated',
);
const direction = fc.constantFrom<SortDirection>('ascending', 'descending');

/** Ideas with a title, a workstream, a gain and an agent, some of them missing, as a table holds. */
const filled = fc
  .uniqueArray(
    fc.record({
      planNumber: fc.integer({min: 1, max: 500}),
      verdict,
      title: fc.constantFrom('Fuse kernels', 'fuse the sampler', 'Pin weights', 'Paged cache'),
      workstreamTitle: fc.option(fc.constantFrom('Decode path', 'GEMM'), {nil: null}),
      agent: fc.constantFrom('', 'impl-1', 'impl-2'),
      updatedMs: when,
      ratio: fc.option(fc.double({min: 0.5, max: 3, noNaN: true}), {nil: null}),
    }),
    {selector: each => each.planNumber, maxLength: 25},
  )
  .map(items =>
    items.map(
      ({ratio, ...fields}): IdeaRow => ({
        workstream: 0,
        gains: ratio === null ? [] : [{metric: 'tok_s', ratio, bestValue: ratio, baselineValue: 1}],
        candidateJobIds: [],
        createdMs: null,
        inRun: true,
        ...fields,
      }),
    ),
  );

const named = (agent: string): string => agent.toUpperCase();

describe('the filter chips, the search and the order of the ideas table', () => {
  it('count the verdicts so that the chips add up to the ideas shown, and a filter shows exactly its chip’s count', () => {
    fc.assert(
      fc.property(rows, all => {
        const counts = verdictCounts(all);
        expect(counts.kept + counts.open + counts.rejected + counts.parked).toBe(all.length);
        for (const which of ['kept', 'open', 'rejected', 'parked'] as const) {
          expect(ideasOfVerdict(all, which)).toHaveLength(counts[which]);
        }
      }),
    );
  });

  it('finds an idea by any word of its title or workstream, or its number, in any case, and finds all with no search', () => {
    fc.assert(
      fc.property(filled, all => {
        expect(ideasMatching(all, '')).toEqual(all);
        expect(ideasMatching(all, '   ')).toEqual(all);
        for (const idea of all) {
          expect(ideasMatching(all, `#${idea.planNumber}`)).toContain(idea);
          expect(ideasMatching(all, idea.title.toUpperCase())).toContain(idea);
        }
        // Narrowing a search never shows more.
        const wide = new Set(ideasMatching(all, 'fuse'));
        for (const idea of ideasMatching(all, 'fuse sampler')) expect(wide.has(idea)).toBe(true);
      }),
      {seed: 71},
    );
  });

  it('orders by any column as a permutation, with rows that have no value last either way and ties in their order', () => {
    fc.assert(
      fc.property(filled, sortKey, direction, (all, key, way) => {
        const sorted = sortIdeas(all, {key, direction: way}, named);
        expect([...sorted].sort((a, b) => a.planNumber - b.planNumber)).toEqual(
          [...all].sort((a, b) => a.planNumber - b.planNumber),
        );
        // Sorting a sorted table again changes nothing: the order is stable and total.
        expect(sortIdeas(sorted, {key, direction: way}, named)).toEqual(sorted);
        if (key === 'gain') {
          const firstMissing = sorted.findIndex(row => row.gains.length === 0);
          if (firstMissing >= 0) {
            expect(sorted.slice(firstMissing).every(row => row.gains.length === 0)).toBe(true);
          }
        }
        if (key === 'id') {
          const ids = sorted.map(row => row.planNumber);
          expect(ids).toEqual([...ids].sort((a, b) => (way === 'ascending' ? a - b : b - a)));
        }
      }),
      {seed: 72},
    );
  });

  it('turns the same column over on a second press and starts another column in its first order', () => {
    fc.assert(
      fc.property(sortKey, sortKey, direction, (current, pressed, way) => {
        const next = nextSort({key: current, direction: way}, pressed);
        expect(next.key).toBe(pressed);
        if (current === pressed) expect(next.direction).not.toBe(way);
      }),
    );
    expect(nextSort({key: 'updated', direction: 'descending'}, 'gain')).toEqual({
      key: 'gain',
      direction: 'descending',
    });
    expect(nextSort({key: 'gain', direction: 'descending'}, 'idea')).toEqual({
      key: 'idea',
      direction: 'ascending',
    });
  });
});
