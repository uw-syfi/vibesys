/**
 * A run that ends shows what it showed while it lived. The desktop reads one projection for a run, live
 * or over: its search state and its summary, whole, as the host serves them. The run's status moving
 * from working to over is one change among the rest, and nothing the screens read of the run's facts
 * (a report's chart, best, candidates, workstreams and ideas, a row's best and candidates, and Agents,
 * the transcript, Cost and Catch me up, which read the run's own records) depends on it.
 */
import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  Direction,
  RunChangeSchema,
  RunSchema,
  RunStatus,
  RunStopReason,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {
  generateScenario,
  measuredSearch,
  type ObjectiveSpec,
  serveSummaries,
} from '../../testing/orchestrator/index.js';
import {DORMANT_GENERIC_RUN_INDEX} from '../generic-run-index.js';
import {fleetRows, measuredObjectives} from './fleet.js';
import {applyChange, applySearch} from './fold.js';
import {agentsStrip} from './report/agents-strip.js';
import {runReport} from './report/run-report.js';
import {emptyState, type OrchestratorState} from './state.js';

/** How a run ends: the status it is left in and the reason it gives. */
const ENDINGS = [
  {status: RunStatus.ENDED, reason: RunStopReason.FINISHED},
  {status: RunStatus.ENDED, reason: RunStopReason.STALLED},
  {status: RunStatus.ENDED, reason: RunStopReason.OPERATOR},
  {status: RunStatus.FAILED, reason: RunStopReason.LEAD_GONE},
] as const;

const objective = fc.record({
  name: fc.constantFrom('throughput', 'p99_latency', 'total_ops_per_sec'),
  direction: fc.constantFrom(Direction.MAXIMIZE, Direction.MINIMIZE),
  unit: fc.constantFrom('ops/s', 'ms', ''),
  base: fc.integer({min: 1, max: 500}),
  best: fc.integer({min: 501, max: 5000}),
});

const objectives = fc.uniqueArray(objective, {
  selector: spec => spec.name,
  minLength: 1,
  maxLength: 2,
}) as fc.Arbitrary<ObjectiveSpec[]>;

/** The facts a run's screens read, as each reads them, with nothing that is the run's status. */
function factsOf(state: OrchestratorState, runId: string) {
  const report = runReport(state, runId, 0);
  const [row] = fleetRows([
    {
      key: 'h',
      label: 'h',
      data: state,
      runIndex: DORMANT_GENERIC_RUN_INDEX,
      freshness: 'live',
      status: {kind: 'live', cause: null, staleForMs: null},
    },
  ]).filter(each => each.runId === runId);
  return {
    objectives: report?.objectives,
    tradeoff: report?.tradeoff,
    ideas: report?.ideas,
    summary: report?.summary,
    searchReading: report?.search.state,
    truncated: report?.truncated,
    row: {
      objectives: row?.objectives,
      candidates: measuredObjectives(
        state.searches[runId],
        state.summaries[runId]?.objectives ?? [],
      ),
      lastReportMs: row?.lastReportMs,
      costUsd: row?.costUsd,
    },
    // The records Agents, the transcript, Cost and Catch me up read: not the run's status.
    agents: Object.keys(state.agents).sort(),
    turns: Object.keys(state.turns).sort(),
    items: Object.keys(state.items).sort(),
    messages: Object.keys(state.messages).sort(),
    checkIns: Object.keys(state.checkIns).sort(),
    costs: state.costs,
    agentTypes: agentsStrip(state, runId).types.map(type => [type.role, type.total]),
  };
}

describe('a run that ends', () => {
  it('shows in its report and its row every fact it showed while it lived, however it ends', () => {
    fc.assert(
      fc.property(
        fc.integer({min: 1, max: 40}),
        fc.integer({min: 0, max: 25}),
        fc.integer({min: 0, max: 8}),
        objectives,
        fc.constantFrom(...ENDINGS),
        (seed, candidates, ideas, specs, ending) => {
          const scenario = generateScenario({seed, subagents: 2, live: true});
          const {runId} = scenario;
          const startMs = scenario.endMs - 600_000;
          let state: OrchestratorState = scenario.changes.reduce(applyChange, emptyState);
          state = applySearch(
            state,
            measuredSearch(runId, {
              seed,
              candidates,
              startMs,
              endMs: scenario.endMs,
              objectives: specs,
              ideas,
            }),
          );
          const live = serveSummaries(state);
          const before = factsOf(live, runId);

          // The run ends: its record says why and when, and the host serves its summary again.
          const run = state.runs[runId];
          if (run === undefined) throw new Error('the scenario has no run');
          const ended = create(RunSchema, {
            ...run,
            status: ending.status,
            stopReason: ending.reason,
            endedAt: timestampFromMs(scenario.endMs),
          });
          const over = serveSummaries(
            applyChange(
              live,
              create(RunChangeSchema, {
                sequence: live.sequence + 1n,
                body: {case: 'run', value: ended},
              }),
            ),
          );
          expect(over.runs[runId]?.status).toBe(ending.status);
          expect(factsOf(over, runId)).toEqual(before);
        },
      ),
    );
  });
});
