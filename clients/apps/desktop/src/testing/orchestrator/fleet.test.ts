import {describe, expect, it} from '@effect/vitest';
import {
  decodeValidated,
  encodeMessage,
  RunChangeSchema,
  RunStatus,
  RunsSnapshotSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {applyChange} from '../../state/orchestrator/fold.js';
import {emptyState} from '../../state/orchestrator/state.js';
import {FakeDaemon, fleetScenario} from './index.js';

const NOW = Date.UTC(2026, 10, 7, 12, 0, 0);

describe('a generated fleet', () => {
  it('every event and the snapshot are valid on the wire, and the sequence has no gaps', () => {
    fc.assert(
      fc.property(
        fc.integer({min: 1, max: 5000}),
        fc.integer({min: 0, max: 30}),
        fc.integer({min: 0, max: 30}),
        (seed, runs, live) => {
          const scenario = fleetScenario({seed, prefix: 'h0', runs, live, nowMs: NOW});
          scenario.changes.forEach((event, index) => {
            expect(event.sequence).toBe(BigInt(index + 1));
            decodeValidated(RunChangeSchema, encodeMessage(RunChangeSchema, event));
          });
          const snapshot = new FakeDaemon(scenario).snapshot();
          const decoded = decodeValidated(
            RunsSnapshotSchema,
            encodeMessage(RunsSnapshotSchema, snapshot),
          );
          expect(decoded.runs).toHaveLength(runs);
        },
      ),
      {numRuns: 60},
    );
  });

  it('has the live runs the options ask for, the rest over, and the same bytes for the same options', () => {
    const options = {seed: 4, prefix: 'h1', runs: 12, live: 5, nowMs: NOW} as const;
    const state = fleetScenario(options).changes.reduce(applyChange, emptyState);
    const runs = Object.values(state.runs);
    const working = runs.filter(run =>
      [RunStatus.RUNNING, RunStatus.STOPPING, RunStatus.STARTING].includes(run.status),
    );
    expect(runs).toHaveLength(12);
    expect(working).toHaveLength(5);
    expect(fleetScenario(options)).toEqual(fleetScenario(options));
  });
});
