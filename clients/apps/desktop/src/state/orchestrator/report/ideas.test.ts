import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {IdeaVerdict, PlanStatus, SearchStateSchema} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {
  type IdeaRow,
  ideasInRun,
  ideasInWindow,
  ideasOfVerdict,
  ideasReport,
  type Verdict,
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
const window = fc
  .tuple(fc.integer({min: 0, max: 1000}), fc.integer({min: 0, max: 1000}))
  .map(([a, b]) => ({fromMs: Math.min(a, b), toMs: Math.max(a, b)}));

describe('the display filters of the ideas table', () => {
  it('"since check-in" keeps exactly the ideas created or changed inside the window, in order', () => {
    fc.assert(
      fc.property(rows, window, (all, span) => {
        const kept = ideasInWindow(all, span);
        const inside = (at: number | null): boolean =>
          at !== null && at > span.fromMs && at <= span.toMs;
        expect(kept).toEqual(all.filter(idea => inside(idea.createdMs) || inside(idea.updatedMs)));
      }),
    );
  });

  it('a wider window never shows fewer ideas, and a window of no length shows none', () => {
    fc.assert(
      fc.property(rows, window, fc.nat(100), fc.nat(100), (all, span, before, after) => {
        const wider = {fromMs: span.fromMs - before, toMs: span.toMs + after};
        const narrow = new Set(ideasInWindow(all, span));
        const wide = new Set(ideasInWindow(all, wider));
        for (const idea of narrow) expect(wide.has(idea)).toBe(true);
        expect(ideasInWindow(all, {fromMs: span.fromMs, toMs: span.fromMs})).toEqual([]);
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
