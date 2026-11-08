import {describe, expect, it} from '@effect/vitest';
import {
  AgentStatus,
  decodeValidated,
  encodeMessage,
  ReportKind,
  RunChangeSchema,
  RunLimit,
  RunStatus,
  RunsSnapshotSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {applyChange} from '../../state/orchestrator/fold.js';
import {emptyState} from '../../state/orchestrator/state.js';
import {ms} from '../../state/orchestrator/time.js';
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

  it('has a live run for each reason a run wants a look, each as the host would hold it, and none among the runs that are over', () => {
    fc.assert(
      fc.property(fc.integer({min: 1, max: 5000}), seed => {
        const scenario = fleetScenario({seed, prefix: 'h2', runs: 14, live: 7, nowMs: NOW});
        const state = scenario.changes.reduce(applyChange, emptyState);
        const runs = Object.values(state.runs);
        const live = runs.filter(run => run.endedAt === undefined);
        const stuck = new Set(
          scenario.searches.filter(s => s.troubles.length > 0).map(s => s.runId),
        );
        const [stalled, ...moreStalled] = live.filter(run => run.stalledSince !== undefined);
        const [final, ...moreFinal] = live.filter(run => run.finalReported);
        expect([moreStalled, moreFinal]).toEqual([[], []]);
        expect(live.filter(run => run.limitReached === RunLimit.COST)).toHaveLength(1);
        expect(live.filter(run => run.limitReached === RunLimit.TIME)).toHaveLength(1);
        expect(live.filter(run => stuck.has(run.id))).toHaveLength(1);
        // Nothing works in a stalled run, and a run whose lead reported its end has that report.
        const agents = Object.values(state.agents);
        expect(
          agents.filter(a => a.runId === stalled?.id && a.status === AgentStatus.ACTIVE),
        ).toEqual([]);
        const reports = Object.values(state.messages).filter(m => m.runId === final?.id);
        expect(reports.map(m => m.body)).toEqual([
          {case: 'report', value: expect.objectContaining({kind: ReportKind.FINAL})},
        ]);
        const over = runs.filter(run => run.endedAt !== undefined);
        expect(over).toHaveLength(7);
        for (const run of over) {
          expect(run.stalledSince ?? run.limitReachedAt).toBeUndefined();
          expect(run.finalReported || stuck.has(run.id)).toBe(false);
        }
      }),
      {numRuns: 40},
    );
  });

  it('has every run begin before it ends and end before the fleet is read', () => {
    fc.assert(
      fc.property(fc.integer({min: 1, max: 5000}), fc.integer({min: 1, max: 40}), (seed, runs) => {
        const scenario = fleetScenario({seed, prefix: 'h0', runs, live: 0, nowMs: NOW});
        const state = scenario.changes.reduce(applyChange, emptyState);
        for (const run of Object.values(state.runs)) {
          const start = ms(run.createdAt) ?? Number.NaN;
          const end = ms(run.endedAt) ?? Number.NaN;
          expect(start).toBeLessThan(end);
          expect(end).toBeLessThan(NOW);
        }
      }),
    );
  });
});
