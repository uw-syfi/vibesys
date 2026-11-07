/**
 * The summary of a run as the host serves it, for the Fake host: the host's rules written once, in test
 * code, so that a scripted world serves what the real one would. The app derives none of this (it reads
 * `RunSummary`); the Fake is the only place besides the host that knows how it is made, and it is held to
 * the host's own rules by the conformance corpus the two share.
 *
 * - The report is the lead's newest report about the whole run: by number, never a window report.
 * - An agent is working when it is launching, in a turn, or waiting on subagents.
 * - The head of each objective is the head's value with the standing bound's unit and share, the
 *   measured objectives first in the order the task declares them, then those with only a bound.
 */
import {create} from '@bufbuild/protobuf';
import {
  type Agent,
  type AgentRole,
  AgentStatus,
  type Message,
  type ObjectiveHead,
  ObjectiveHeadSchema,
  oneofIs,
  ReportScope,
  RoleAgentsSchema,
  type RunSummary,
  RunSummarySchema,
  type SearchState,
} from '@vibesys/orchestrator-proto';
import type {OrchestratorState} from '../../state/orchestrator/state.js';

const WORKING: ReadonlySet<AgentStatus> = new Set([
  AgentStatus.LAUNCHING,
  AgentStatus.ACTIVE,
  AgentStatus.WAITING,
]);

const REPORT_KINDS = {
  text: true,
  handoff: true,
  report: true,
  jobFinished: true,
  subagentSettled: true,
  brief: true,
  leadBrief: true,
  handoffReminder: true,
  leadIdleReminder: true,
  reportDue: true,
  catchUpRequest: true,
  runLimitReached: true,
} as const;

/** The report the run is summed up by: the highest-numbered one that is about the whole run. */
export function summaryMessage(messages: readonly Message[]): Message | undefined {
  let best: Message | undefined;
  for (const message of messages) {
    if (!oneofIs(message.body, 'report', REPORT_KINDS)) continue;
    if (message.body.value.scope === ReportScope.WINDOW) continue;
    const held = best?.body.case === 'report' ? best.body.value.sequence : -1;
    if (best === undefined || message.body.value.sequence >= held) best = message;
  }
  return best;
}

const finite = (value: number | undefined): number | undefined =>
  value !== undefined && Number.isFinite(value) ? value : undefined;

/** The head's value on each objective, as the host serves it. */
export function servedHeads(search: SearchState | undefined): ObjectiveHead[] {
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
  ].filter(row => row.name !== '');
  return rows.map(({name, value}) => {
    const bound = bounds.get(name);
    return create(ObjectiveHeadSchema, {
      name,
      value: bound?.headValue === undefined ? value : finite(bound.headValue),
      unit: bound?.versions.at(-1)?.unit ?? '',
      percentOfBound: finite(bound?.percentOfBound),
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
    objectives: servedHeads(search),
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
