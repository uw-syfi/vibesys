import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {JobState, SearchStateSchema, VerdictKind} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {candidates, jobQueue, jobsWithNoRun, planStatus, resultSoFar} from './search-views.js';

it('views preserve the facts while unreadable and only accept judgments against the current head', () => {
  fc.assert(
    fc.property(
      fc.constantFrom(
        VerdictKind.BETTER,
        VerdictKind.WORSE,
        VerdictKind.WITHIN_NOISE,
        VerdictKind.INCONCLUSIVE,
        VerdictKind.UNSPECIFIED,
      ),
      fc.boolean(),
      (kind, current) => {
        const search = create(SearchStateSchema, {
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
          unattributedJobs: 7,
        });
        const [candidate] = candidates(search);
        expect(candidate?.result).toBe(search.results[0]);
        expect(candidate?.stale).toBe(!current);
        if (!current) expect(candidate?.label).toBe('Not judged');
        expect(resultSoFar(search)).toBe(search.head?.state);
        expect(jobsWithNoRun(search)).toBe(7);
        expect(planStatus(search)).toEqual([]);
      },
    ),
    {seed: 99},
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
