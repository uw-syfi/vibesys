/**
 * Reads of the state for the screens: the agent tree, an agent's turns and thread, the timeline,
 * and fan-out over time. Pure functions of the state and, where a running thing has no end
 * yet, of `nowMs`, which the caller passes (no clock here).
 */
import {
  type Agent,
  AgentRole,
  AgentStatus,
  type Item,
  ItemStatus,
  JobOutcome,
  type Message,
  oneofIs,
  type Turn,
  TurnStatus,
} from '@vibesys/orchestrator-proto';
import {messageIs} from './messages.js';
import type {OrchestratorState} from './state.js';
import {ms} from './time.js';

const startOf = (agent: Agent): number => ms(agent.startedAt) ?? 0;

export function runAgents(state: OrchestratorState, runId: string): Agent[] {
  return Object.values(state.agents)
    .filter(agent => agent.runId === runId)
    .sort((a, b) => startOf(a) - startOf(b) || a.id.localeCompare(b.id));
}

export interface TreeNode {
  readonly agent: Agent;
  readonly depth: number;
  readonly children: readonly TreeNode[];
}

/** The agents of a run as a forest: the lead at the root, each agent under the one that spawned it. */
export function agentTree(state: OrchestratorState, runId: string): TreeNode[] {
  const agents = runAgents(state, runId);
  const known = new Set(agents.map(agent => agent.id));
  const build = (agent: Agent, depth: number): TreeNode => ({
    agent,
    depth,
    children: agents
      .filter(child => child.parentAgentId === agent.id)
      .map(child => build(child, depth + 1)),
  });
  return agents
    .filter(agent => agent.parentAgentId === '' || !known.has(agent.parentAgentId))
    .map(agent => build(agent, 0));
}

/** The tree flattened top to bottom, for a list. */
export function flattenTree(nodes: readonly TreeNode[]): TreeNode[] {
  return nodes.flatMap(node => [node, ...flattenTree(node.children)]);
}

export function agentTurns(state: OrchestratorState, agentId: string): Turn[] {
  return Object.values(state.turns)
    .filter(turn => turn.agentId === agentId)
    .sort((a, b) => a.ordinal - b.ordinal);
}

/** One item as shown: the stored item with any unstored progress laid over it. */
export interface ThreadItem {
  readonly item: Item;
  /** Text or command output so far, stored plus progress. */
  readonly text: string;
  readonly output: string;
  readonly live: boolean;
}

export interface ThreadTurn {
  readonly turn: Turn | null;
  readonly turnId: string;
  readonly items: readonly ThreadItem[];
  /** Earlier items of this agent are not loaded between the previous turn and this one. */
  readonly missingBefore: boolean;
}

function shown(state: OrchestratorState, item: Item): ThreadItem {
  const overlay = state.overlays[item.id];
  const body = item.body;
  const stored = oneofIs(body, 'tool', {assistant: true, reasoning: true, prompt: true, tool: true})
    ? ''
    : oneofIs(body, undefined, {assistant: true, reasoning: true, prompt: true})
      ? ''
      : body.value.text;
  const output = oneofIs(body, 'tool', {assistant: true, reasoning: true, prompt: true, tool: true})
    ? body.value.output
    : '';
  return {
    item,
    text: stored + (overlay?.text ?? ''),
    output: output + (overlay?.commandOutput ?? ''),
    live: item.status === ItemStatus.RUNNING,
  };
}

/**
 * Where the newest unbroken run of an agent's loaded items starts, or null when none is loaded.
 * Reading older items below it first fills any hole left while the screen was away, then goes on
 * to the beginning, so one "Show earlier steps" action reaches every step.
 */
export function unbrokenFrom(state: OrchestratorState, agentId: string): number | null {
  const ordinals = new Set(
    Object.values(state.items)
      .filter(item => item.agentId === agentId)
      .map(item => item.ordinal),
  );
  if (ordinals.size === 0) return null;
  let from = Math.max(...ordinals);
  while (ordinals.has(from - 1)) from -= 1;
  return from;
}

/** The agent's items by turn, oldest first: past and live are the same view. */
export function agentThread(state: OrchestratorState, agentId: string): ThreadTurn[] {
  const items = Object.values(state.items)
    .filter(item => item.agentId === agentId)
    .sort((a, b) => a.ordinal - b.ordinal);
  const groups: {turnId: string; items: Item[]}[] = [];
  for (const item of items) {
    const last = groups[groups.length - 1];
    if (last !== undefined && last.turnId === item.turnId) last.items.push(item);
    else groups.push({turnId: item.turnId, items: [item]});
  }
  let previous: number | null = null;
  return groups.map(group => {
    const first = group.items[0]?.ordinal ?? 0;
    const missingBefore = previous !== null && first !== previous + 1;
    previous = group.items[group.items.length - 1]?.ordinal ?? previous;
    return {
      turn: state.turns[group.turnId] ?? null,
      turnId: group.turnId,
      items: group.items.map(item => shown(state, item)),
      missingBefore,
    };
  });
}

/** Messages to or from an agent, oldest first. */
export function messagesOf(state: OrchestratorState, agentId: string): Message[] {
  return Object.values(state.messages)
    .filter(message => message.toAgentId === agentId || message.fromAgentId === agentId)
    .sort((a, b) => (ms(a.at) ?? 0) - (ms(b.at) ?? 0) || a.id.localeCompare(b.id));
}

export interface Bar {
  readonly startMs: number;
  readonly endMs: number;
  /** Still running at `nowMs`. */
  readonly running: boolean;
  readonly status: TurnStatus;
  readonly turnId: string;
}

export interface TimelineRow {
  readonly agent: Agent;
  readonly depth: number;
  readonly bars: readonly Bar[];
}

export interface JobBar {
  readonly jobId: string;
  readonly outcome: JobOutcome;
  readonly submittedBy: string;
  readonly startMs: number | null;
  readonly endMs: number;
  readonly detail: string;
}

export interface Timeline {
  readonly startMs: number;
  readonly endMs: number;
  readonly rows: readonly TimelineRow[];
  readonly jobs: readonly JobBar[];
}

/** When each agent ran, and the jobs underneath. */
export function timeline(state: OrchestratorState, runId: string, nowMs: number): Timeline {
  const run = state.runs[runId];
  const rows = flattenTree(agentTree(state, runId)).map(({agent, depth}) => ({
    agent,
    depth,
    bars: agentTurns(state, agent.id).map((turn): Bar => {
      const startMs = ms(turn.startedAt) ?? nowMs;
      const running = turn.status === TurnStatus.RUNNING;
      return {
        startMs,
        endMs: ms(turn.endedAt) ?? nowMs,
        running,
        status: turn.status,
        turnId: turn.id,
      };
    }),
  }));
  const jobs = Object.values(state.messages)
    .filter(message => message.runId === runId && messageIs(message, 'jobFinished'))
    .flatMap((message): JobBar[] => {
      const job = messageIs(message, 'jobFinished') ? message.body.value : null;
      const fact = state.searches[runId]?.jobs.find(candidate => candidate.id === job?.jobId);
      const startMs = ms(fact?.startedAt) ?? ms(fact?.submittedAt);
      const endMs = ms(fact?.finishedAt) ?? ms(message.at);
      // Missing timing is unknown, including jobs outside the producer's retained window.
      if (endMs === null) return [];
      return [
        {
          jobId: job?.jobId ?? '',
          outcome: job?.outcome ?? JobOutcome.UNSPECIFIED,
          submittedBy: job?.submittedByAgentId ?? '',
          startMs,
          endMs,
          detail: job?.detail ?? '',
        },
      ];
    })
    .sort(
      (a, b) => (a.startMs ?? a.endMs) - (b.startMs ?? b.endMs) || a.jobId.localeCompare(b.jobId),
    );
  const all = [
    ms(run?.createdAt) ?? nowMs,
    ...rows.flatMap(row => row.bars.map(bar => bar.startMs)),
    ...jobs.map(job => job.startMs ?? job.endMs),
  ];
  const ends = [
    ms(run?.endedAt) ?? nowMs,
    ...rows.flatMap(row => row.bars.map(bar => bar.endMs)),
    ...jobs.map(job => job.endMs),
  ];
  return {startMs: Math.min(...all), endMs: Math.max(...ends), rows, jobs};
}

const FINAL_AGENT = new Set([AgentStatus.ENDED, AgentStatus.FAILED, AgentStatus.LOST]);

export interface FanoutPoint {
  readonly atMs: number;
  /** Agents other than the lead whose session was open from here until the next point. */
  readonly live: number;
}

/**
 * How many subagents were open over time, as a step function. An agent counts from its start to
 * its end; one that is over but has no end time counts until its last turn ended, and one still
 * open has no end at all.
 */
export function fanoutOverTime(state: OrchestratorState, runId: string): FanoutPoint[] {
  const spans = runAgents(state, runId)
    .filter(agent => agent.role !== AgentRole.LEAD)
    .map(agent => {
      const start = startOf(agent);
      const lastTurnEnd = Math.max(
        start,
        ...agentTurns(state, agent.id).map(turn => ms(turn.endedAt) ?? start),
      );
      const over = FINAL_AGENT.has(agent.status) || agent.endedAt !== undefined;
      // An agent still open has no end: it counts to the last point, so the line does not fall
      // at "now" for work that is still going.
      return {start, end: over ? (ms(agent.endedAt) ?? lastTurnEnd) : null};
    });
  const edges = [
    ...spans.map(span => ({at: span.start, delta: 1})),
    ...spans.flatMap(span => (span.end === null ? [] : [{at: span.end, delta: -1}])),
  ].sort((a, b) => a.at - b.at || a.delta - b.delta);
  const points: FanoutPoint[] = [];
  let live = 0;
  for (const edge of edges) {
    live += edge.delta;
    const last = points[points.length - 1];
    if (last !== undefined && last.atMs === edge.at)
      points[points.length - 1] = {atMs: edge.at, live};
    else points.push({atMs: edge.at, live});
  }
  return points;
}
