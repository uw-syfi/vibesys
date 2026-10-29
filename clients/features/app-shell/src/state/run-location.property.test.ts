import {expect, test} from 'bun:test';
import {recordedProjectRoot} from '@vibesys/hosts';
import fc from 'fast-check';
import {runProjectRoot, SIDEBAR_EMPTY} from './index.js';

test('property: run addressing survives registry expiry and catalog refresh without journal metadata', () => {
  fc.assert(
    fc.property(
      fc.integer({min: 1, max: 10000}),
      fc.boolean(),
      fc.boolean(),
      (id, catalog, saved) => {
        const run = {host: 'local', runId: `run-${id}`};
        const project = `/projects/project-${id}`;
        const recent = {
          ...run,
          hostLabel: 'host-a',
          instanceId: null,
          project,
          task: null,
          attachedAt: 0,
        };
        const state = {
          ...SIDEBAR_EMPTY,
          recent: saved ? [recent] : [],
          allRuns: {
            ...SIDEBAR_EMPTY.allRuns,
            projects: catalog
              ? {
                  [JSON.stringify(['local', project])]: {
                    kind: 'listed' as const,
                    runs: [{runId: run.runId, task: null, createdAt: 0}],
                  },
                }
              : {},
          },
        };
        expect(runProjectRoot(state, run)).toBe(
          catalog || saved ? recordedProjectRoot(project) : undefined,
        );
        const refreshed = {
          ...state,
          allRuns: {
            ...state.allRuns,
            stale: catalog
              ? {
                  [JSON.stringify(['local', project])]: [
                    {runId: run.runId, task: null, createdAt: 0},
                  ],
                }
              : {},
            projects: {},
          },
        };
        expect(runProjectRoot(refreshed, run)).toBe(
          catalog || saved ? recordedProjectRoot(project) : undefined,
        );
      },
    ),
    {numRuns: 24, seed: 34370},
  );
});
