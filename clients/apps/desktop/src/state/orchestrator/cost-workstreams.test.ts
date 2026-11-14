import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {RunSchema, RunStatus, SearchStateSchema} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {emptyState, exploredWorkstreams} from '../index.js';

const run = (id: string) =>
  create(RunSchema, {
    id,
    repository: '/repo',
    task: `Task ${id}`,
    status: RunStatus.RUNNING,
    leadAgentId: `${id}-lead`,
    createdAt: timestampFromMs(1_000),
  });

const search = (runId: string, numbers: readonly number[], truncated = false) =>
  create(SearchStateSchema, {
    runId,
    revision: 1n,
    health: {state: {case: 'liveSince', value: timestampFromMs(1_000)}},
    workstreamViews: numbers.map(number => ({number})),
    truncated,
  });

describe('the public explored-workstreams view', () => {
  it('counts every served named run-workstream and never calls the unattributed zero row one', () => {
    fc.assert(
      fc.property(
        fc.uniqueArray(fc.integer({min: 0, max: 100}), {maxLength: 50}),
        fc.boolean(),
        (numbers, anotherPartWasCut) => {
          const state = {
            ...emptyState,
            runs: {'run-a': run('run-a')},
            searches: {'run-a': search('run-a', numbers, anotherPartWasCut)},
          };
          expect(exploredWorkstreams(state, 1)).toEqual({
            count: numbers.filter(number => number > 0).length,
            complete: true,
          });
        },
      ),
      {seed: 47},
    );
  });

  it('keeps the rows it can prove as a lower bound when a search may have passed its cap', () => {
    const numbers = Array.from({length: 101}, (_, at) => at + 1);
    const state = {
      ...emptyState,
      runs: {'run-a': run('run-a')},
      searches: {'run-a': search('run-a', numbers, true)},
    };
    expect(exploredWorkstreams(state, 1)).toEqual({count: 101, complete: false});
  });

  it('does not call a count complete when a run, search, or independent run count is missing', () => {
    const base = {
      ...emptyState,
      runs: {'run-a': run('run-a'), 'run-b': run('run-b')},
      searches: {'run-a': search('run-a', [1, 2])},
    };
    expect(exploredWorkstreams(base, 2)).toEqual({count: 2, complete: false});
    expect(
      exploredWorkstreams(
        {...base, searches: {...base.searches, 'run-b': search('run-b', [3])}},
        1,
      ),
    ).toEqual({count: 3, complete: false});
    expect(
      exploredWorkstreams(
        {
          ...base,
          searches: {...base.searches, 'run-b': search('run-b', [3])},
          cut: {truncated: true, runsLeftOut: 1},
        },
        2,
      ),
    ).toEqual({count: 3, complete: false});
  });
});
