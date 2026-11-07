/**
 * Everything the run report shows of one run, as one plain value of the folded state: the header's
 * facts, the report the host sums the run up by, the objectives with their history and bounds, the
 * trade-off and the agents by type. A pure function of the state and the time the caller passes.
 */
import type {RunStatus, RunStopReason, SearchState} from '@vibesys/orchestrator-proto';
import {oneofIs} from '@vibesys/orchestrator-proto';
import type {OrchestratorState} from '../state.js';
import {ms} from '../time.js';
import {type AgentsStrip, agentsStrip} from './agents-strip.js';
import {type ObjectiveReport, objectiveReports} from './objectives.js';
import {type SummaryView, summaryReport} from './summary.js';
import {type TradeoffReport, tradeoffReport} from './tradeoff.js';

export interface ReportHeader {
  /** What the run is called: its task. */
  readonly name: string;
  readonly status: RunStatus;
  readonly stopReason: RunStopReason;
  readonly startedMs: number;
  /** When the run ended, or null while it lives. */
  readonly endedMs: number | null;
  /** From its start to its end, or to `nowMs` while it lives. */
  readonly elapsedMs: number;
}

/** Whether the search facts behind the performance are being read now. */
export type SearchReading =
  | {readonly state: 'none'}
  | {readonly state: 'live'}
  | {readonly state: 'unreadable'; readonly sinceMs: number};

export interface RunReport {
  readonly runId: string;
  readonly header: ReportHeader;
  readonly summary: SummaryView | undefined;
  readonly objectives: readonly ObjectiveReport[];
  readonly tradeoff: TradeoffReport | undefined;
  readonly agents: AgentsStrip;
  readonly search: SearchReading;
  /** The span of the charts' time axis: the run's start to its end, or to now while it lives. */
  readonly span: {readonly fromMs: number; readonly toMs: number};
  /** The search state served more than fits and left some out. */
  readonly truncated: boolean;
}

function searchReading(search: SearchState | undefined): SearchReading {
  const health = search?.health?.state;
  if (search === undefined) return {state: 'none'};
  return oneofIs(health, 'unreadable', {liveSince: true, unreadable: true})
    ? {state: 'unreadable', sinceMs: ms(health.value.since) ?? 0}
    : {state: 'live'};
}

/** The report of a run, or undefined when the run is not in the state. */
export function runReport(
  state: OrchestratorState,
  runId: string,
  nowMs: number,
): RunReport | undefined {
  const run = state.runs[runId];
  if (run === undefined) return undefined;
  const search = state.searches[runId];
  const startedMs = ms(run.createdAt) ?? nowMs;
  const endedMs = ms(run.endedAt);
  const toMs = Math.max(startedMs, endedMs ?? nowMs);
  return {
    runId,
    header: {
      name: run.task,
      status: run.status,
      stopReason: run.stopReason,
      startedMs,
      endedMs,
      elapsedMs: toMs - startedMs,
    },
    summary: summaryReport(state, runId),
    objectives:
      search === undefined
        ? []
        : objectiveReports(search, state.summaries[runId]?.objectives ?? []),
    tradeoff: search === undefined ? undefined : tradeoffReport(search),
    agents: agentsStrip(state, runId),
    search: searchReading(search),
    span: {fromMs: startedMs, toMs},
    truncated: search?.truncated ?? false,
  };
}
