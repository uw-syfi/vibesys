/**
 * The summary of a run as the host serves it, for the Fake host: the host's rules written once, in test
 * code, so that a scripted world serves what the real one would. The app derives none of this (it reads
 * `RunSummary`); the Fake is the only place besides the host that knows how it is made, and it is held to
 * the host's own rules by the conformance corpus the two share.
 *
 * - The report is the lead's newest report about the whole run: by number, never a window report.
 * - An agent is working when it is launching, in a turn, or waiting on subagents.
 * - Where the run stands on each objective is its head (the head's value) and its best (the bound's, or
 *   where the series' best line ends), with the standing bound's unit and the share each is at: the
 *   measured objectives first in the order the task declares them, then those with only a bound.
 */
import {create} from '@bufbuild/protobuf';
import {
  type Agent,
  type AgentRole,
  AgentStatus,
  type Message,
  type ObjectiveStanding,
  ObjectiveStandingSchema,
  oneofIs,
  ReportScope,
  RoleAgentsSchema,
  type RunSummary,
  RunSummarySchema,
  type SearchState,
} from '@vibesys/orchestrator-proto';
import {messageIs} from '../../state/orchestrator/messages.js';
import type {OrchestratorState} from '../../state/orchestrator/state.js';

const WORKING: ReadonlySet<AgentStatus> = new Set([
  AgentStatus.LAUNCHING,
  AgentStatus.ACTIVE,
  AgentStatus.WAITING,
]);

/** The report the run is summed up by: the highest-numbered one that is about the whole run. */
function summaryMessage(messages: readonly Message[]): Message | undefined {
  let best: Message | undefined;
  let heldSequence = -1;
  for (const message of messages) {
    if (!messageIs(message, 'report') || message.body.value.scope === ReportScope.WINDOW) continue;
    if (best === undefined || message.body.value.sequence >= heldSequence) {
      best = message;
      heldSequence = message.body.value.sequence;
    }
  }
  return best;
}

const finite = (value: number | undefined): number | undefined =>
  value !== undefined && Number.isFinite(value) ? value : undefined;

/** Where the run stands on each objective, its head and its best, as the host serves it. */
function servedStandings(search: SearchState | undefined): ObjectiveStanding[] {
  if (search === undefined) return [];
  const head = search.head?.state;
  const outcome = oneofIs(head, 'current', {current: true, none: true})
    ? head.value.result?.outcome
    : undefined;
  const measure = oneofIs(outcome, 'measure', {check: true, measure: true})
    ? outcome.value
    : undefined;
  const bounds = new Map(search.bounds.map(entry => [entry.objective, entry] as const));
  const measured: {name: string; value: number | undefined}[] = [];
  if (measure !== undefined) {
    const scored = measure.metric?.name ?? '';
    const names = measure.objectives?.metrics.map(metric => metric.name) ?? [scored];
    for (const name of names) {
      measured.push({
        name,
        value:
          name === scored
            ? finite(measure.value)
            : finite(measure.observations.find(each => each.name === name)?.value),
      });
    }
  }
  const named = new Set(measured.map(each => each.name));
  const rows = [
    ...measured,
    ...search.bounds
      .filter(entry => !named.has(entry.objective))
      .map(entry => ({name: entry.objective, value: undefined})),
  ];
  const listed = new Set(rows.map(row => row.name));
  for (const series of search.series) {
    if (!listed.has(series.objective)) rows.push({name: series.objective, value: undefined});
  }
  return rows
    .filter(row => row.name !== '')
    .map(({name, value}) => {
      const bound = bounds.get(name);
      const marked = search.series
        .find(each => each.objective === name)
        ?.points.filter(point => point.bestSoFar)
        .at(-1);
      return create(ObjectiveStandingSchema, {
        name,
        headValue: bound?.headValue === undefined ? value : finite(bound.headValue),
        unit: bound?.versions.at(-1)?.unit ?? '',
        headPercentOfBound: finite(bound?.percentOfBound),
        bestValue: bound?.bestValue === undefined ? finite(marked?.value) : finite(bound.bestValue),
        bestPercentOfBound: finite(bound?.bestPercentOfBound),
      });
    });
}

/** The summary of `runId` the Fake host serves for `state`. */
export function servedSummary(
  state: OrchestratorState,
  runId: string,
  search: SearchState | undefined,
): RunSummary {
  const messages = Object.values(state.messages).filter(message => message.runId === runId);
  const agents: Agent[] = Object.values(state.agents)
    .filter(agent => agent.runId === runId)
    .sort((a, b) => (a.id < b.id ? -1 : 1));
  const roles = [...new Set(agents.map(agent => agent.role))].sort(
    (a: AgentRole, b: AgentRole) => a - b,
  );
  return create(RunSummarySchema, {
    runId,
    report: summaryMessage(messages),
    agentsWorking: agents.filter(agent => WORKING.has(agent.status)).length,
    agentsTotal: agents.length,
    roles: roles.map(role => {
      const ofRole = agents.filter(agent => agent.role === role);
      return create(RoleAgentsSchema, {
        role,
        total: ofRole.length,
        working: ofRole.filter(agent => WORKING.has(agent.status)).length,
      });
    }),
    objectives: servedStandings(search),
  });
}

/**
 * `state` as a client holds it when the host has served the summary of every run in it: for a test
 * that builds a state by folding a scenario's changes, which carry no summary of their own.
 */
export function serveSummaries(state: OrchestratorState): OrchestratorState {
  return {
    ...state,
    summaries: Object.fromEntries(
      Object.keys(state.runs).map(runId => [
        runId,
        servedSummary(state, runId, state.searches[runId]),
      ]),
    ),
  };
}
