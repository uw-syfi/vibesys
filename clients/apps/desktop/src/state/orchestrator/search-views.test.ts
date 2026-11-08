import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  JobState,
  PlanStatus,
  SearchStateSchema,
  UnreadableReason,
  VerdictKind,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {candidates, jobQueue, jobsWithNoRun, planStatus} from './search-views.js';

it('views preserve the facts while unreadable and only accept judgments against the current head', () => {
  fc.assert(
    fc.property(
      fc.constantFrom(
        VerdictKind.BETTER,
        VerdictKind.WORSE,
        VerdictKind.WITHIN_NOISE,
        VerdictKind.INCONCLUSIVE,
        VerdictKind.TRADEOFF,
        VerdictKind.UNSPECIFIED,
      ),
      fc.boolean(),
      (kind, current) => {
        const search = create(SearchStateSchema, {
          health: {state: {case: 'unreadable', value: {reason: UnreadableReason.STREAM_LOST}}},
          head: {state: {case: 'none', value: {head: 'head'}}},
          results: [
            {jobId: 'j1', commit: 'candidate', outcome: {case: 'measure', value: {value: 17}}},
          ],
          judgments: [
            {
              commit: 'candidate',
              against: current ? 'head' : 'old',
              outcome: {case: 'comparison', value: {verdict: {kind}}},
            },
          ],
          plans: [{number: 1, status: PlanStatus.CHOSEN}],
          unattributedJobs: 7,
        });
        const [candidate] = candidates(search);
        expect(candidate?.result).toBe(search.results[0]);
        expect(candidate?.stale).toBe(!current);
        expect(candidate?.label).toBe(
          !current
            ? 'Not judged'
            : {
                [VerdictKind.BETTER]: 'Better',
                [VerdictKind.WORSE]: 'Worse',
                [VerdictKind.WITHIN_NOISE]: 'Within noise',
                [VerdictKind.INCONCLUSIVE]: 'Inconclusive',
                [VerdictKind.TRADEOFF]: 'Tradeoff',
                [VerdictKind.UNSPECIFIED]: 'Not judged',
              }[kind],
        );
        expect(jobsWithNoRun(search)).toBe(7);
        expect(planStatus(search)).toEqual([{plan: search.plans[0], label: 'Chosen'}]);
      },
    ),
    {seed: 99},
  );
});

it('labels a verdict kind or plan status this build does not know, instead of showing nothing', () => {
  fc.assert(
    fc.property(
      // A value past the last known one of each enum: a newer host can name one, and the fields it
      // rides (Verdict.kind, Plan.status) are not pinned to the known set, so it survives decoding.
      fc.integer({min: 6, max: 2_147_483_647}),
      fc.integer({min: 6, max: 2_147_483_647}),
      (unknownVerdict, unknownStatus) => {
        const search = create(SearchStateSchema, {
          head: {state: {case: 'none', value: {head: 'head'}}},
          results: [
            {jobId: 'j1', commit: 'candidate', outcome: {case: 'measure', value: {value: 1}}},
          ],
          judgments: [
            {
              commit: 'candidate',
              against: 'head',
              outcome: {
                case: 'comparison',
                value: {verdict: {kind: unknownVerdict as VerdictKind}},
              },
            },
          ],
          plans: [{number: 1, status: unknownStatus as PlanStatus}],
        });
        expect(candidates(search).map(row => row.label)).toEqual(['Not judged']);
        expect(planStatus(search).map(row => row.label)).toEqual(['Unknown']);
      },
    ),
  );
});

describe('job queue time', () => {
  it('uses typed timestamps and includes only active jobs, at any injected time', () => {
    fc.assert(
      fc.property(fc.integer({min: 0, max: 100000}), now => {
        const search = create(SearchStateSchema, {
          jobs: Object.values(JobState)
            .filter((value): value is JobState => typeof value === 'number')
            .map(state => ({
              id: String(state),
              state,
              submittedAt: timestampFromMs(10),
              startedAt: state === JobState.RUNNING ? timestampFromMs(20) : undefined,
            })),
        });
        const queue = jobQueue(search, now);
        expect(queue.map(row => row.job.state)).toEqual([JobState.QUEUED, JobState.RUNNING]);
        expect(queue.map(row => row.elapsedMs)).toEqual([
          Math.max(0, now - 10),
          Math.max(0, now - 20),
        ]);
      }),
      {seed: 29},
    );
  });
});

it('separates measured candidates, refused judgments, and judgments for a previous head', () => {
  fc.assert(
    fc.property(fc.boolean(), current => {
      const search = create(SearchStateSchema, {
        head: {state: {case: 'none', value: {head: 'head'}}},
        results: [
          {commit: 'measured', outcome: {case: 'measure', value: {value: 1}}},
          {commit: 'other', outcome: {case: 'measure', value: {value: 2}}},
          {commit: 'failed'},
        ],
        judgments: [
          {
            commit: 'measured',
            against: current ? 'head' : 'old',
            outcome: {case: 'refused', value: {}},
          },
        ],
      });
      expect(candidates(search).map(row => [row.result.commit, row.label, row.stale])).toEqual([
        ['measured', current ? 'Comparison unavailable' : 'Not judged', !current],
        ['other', 'Not judged', false],
      ]);
    }),
    {seed: 39},
  );
});
