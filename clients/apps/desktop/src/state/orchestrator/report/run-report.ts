/**
 * Everything the run report shows of one run, as one plain value of the folded state: the header's
 * facts, the report the host sums the run up by, the objectives with their history and bounds, the
 * trade-off and the agents by type. A pure function of the state and the time the caller passes.
 */
import type {
  RunStatus,
  RunStopReason,
  SearchState,
  TaskCard,
  UnreadableReason,
} from '@vibesys/orchestrator-proto';
import {oneofIs} from '@vibesys/orchestrator-proto';
import type {OrchestratorState} from '../state.js';
import {ms} from '../time.js';
import {type AgentsStrip, agentsStrip} from './agents-strip.js';
import {type RunFacts, runFacts} from './facts.js';
import {type IdeasReport, ideasReport} from './ideas.js';
import {type LatestMeasurement, latestMeasurements} from './latest.js';
import {type ObjectiveReport, objectiveReports} from './objectives.js';
import {type PlansReport, plansReport} from './plans.js';
import {type SummaryView, summaryReport} from './summary.js';
import {type TradeoffReport, tradeoffReport} from './tradeoff.js';

export interface ReportHeader {
  /** What the run is called: its task. */
  readonly name: string;
  /** The task card the host stored on the run; null for a run recorded before cards. */
  readonly card: TaskCard | null;
  readonly status: RunStatus;
  readonly stopReason: RunStopReason;
  readonly startedMs: number;
  /** When the run ended, or null while it lives. */
  readonly endedMs: number | null;
  /** From its start to its end, or to `nowMs` while it lives. */
  readonly elapsedMs: number;
}

/** Whether the search facts behind the performance are being read now, and if not, why. */
export type SearchReading =
  | {readonly state: 'none'}
  | {readonly state: 'live'}
  | {
      readonly state: 'unreadable';
      /** Why the host cannot read the run's job log now. */
      readonly reason: UnreadableReason;
      /** The host's own words about it, to be shown as they are; empty when it sent none. */
      readonly detail: string;
      /** Since when the host has said so, or null when its message says no time. */
      readonly sinceMs: number | null;
    };

export interface RunReport {
  readonly runId: string;
  readonly header: ReportHeader;
  readonly summary: SummaryView | undefined;
  readonly objectives: readonly ObjectiveReport[];
  readonly tradeoff: TradeoffReport | undefined;
  /** The run's ideas and workstreams, once a search state has been served. */
  readonly ideas: IdeasReport | undefined;
  /** The run's plans counted by status and the jobs no run claimed, once a search state has been served. */
  readonly plans: PlansReport | undefined;
  readonly agents: AgentsStrip;
  /** The facts line's numbers: the whole run in four counts. */
  readonly facts: RunFacts;
  /** The newest measurements, newest first, one value for each of `objectives`. */
  readonly latest: readonly LatestMeasurement[];
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
    ? {
        state: 'unreadable',
        reason: health.value.reason,
        detail: health.value.detail,
        sinceMs: ms(health.value.since),
      }
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
  const objectives = objectiveReports(search, state.summaries[runId]?.objectives ?? []);
  const ideas = ideasReport(search);
  const agents = agentsStrip(state, runId);
  return {
    runId,
    header: {
      name: run.task,
      card: run.taskCard ?? null,
      status: run.status,
      stopReason: run.stopReason,
      startedMs,
      endedMs,
      elapsedMs: toMs - startedMs,
    },
    summary: summaryReport(state, runId),
    objectives,
    tradeoff: search === undefined ? undefined : tradeoffReport(search),
    ideas,
    plans: plansReport(search),
    agents,
    facts: runFacts(search, ideas, agents, state.costs[runId]),
    latest: latestMeasurements(objectives, ideas?.plansOfJob),
    search: searchReading(search),
    span: {fromMs: startedMs, toMs},
    truncated: search?.truncated ?? false,
  };
}
