/**
 * The spawn tree of a run as rows over time: one row per agent from its start to its end, nested
 * under the agent that spawned it, with a tick on the parent where each child's result came back.
 * A parent that spawned many agents of one role folds them into a single count bar (`worker ×12`),
 * and any agent can fold everything under it. A pure function of the state, the host's name, the
 * fold choices and `nowMs` (no clock here).
 *
 * Properties the tests hold (`spawn-tree.properties.test.ts`), for any agents, any parent links
 * (cycles and missing parents included) and any fold choices:
 * - every agent is covered exactly once: it is an agent row, or it is counted by the one folded row
 *   that hides it;
 * - an agent's row follows its parent's row, one level deeper (a group's members one level deeper
 *   than the group), and a folded row has none under it;
 * - the count of a folded row is the number of agents it hides;
 * - every row has start no later than end, a group spans its agents, and a tick lies on the bar of
 *   the parent it belongs to.
 */
import {
  type Agent,
  type AgentRole,
  AgentStatus,
  type Harness,
  type Message,
  RunStatus,
} from '@vibesys/orchestrator-proto';
import {noteOf} from '../orchestrator/messages.js';
import type {OrchestratorState} from '../orchestrator/state.js';
import {ms} from '../orchestrator/time.js';
import {type AgentName, agentNames, startOrder} from './names.js';

/** Where an agent stands, as a bar's color: working, finished, or failed. */
export type SpawnState = 'running' | 'done' | 'failed';

/** Whether a row is the last of its siblings, which decides the line that joins it to its parent. */
export type Branch = 'more' | 'last';

/** A child's result coming back to its parent. */
export interface Tick {
  readonly atMs: number;
  readonly childId: string;
  readonly tone: 'ok' | 'failed';
}

interface RowBase {
  /** Names the row for its fold choice and as a list key; unique among the rows of one tree. */
  readonly key: string;
  readonly depth: number;
  /**
   * One entry per level from the root: whether that ancestor, or the row itself at the last
   * entry, is the last of its siblings. Its length is `depth`.
   */
  readonly lineage: readonly Branch[];
  readonly state: SpawnState;
  readonly startMs: number;
  readonly endMs: number;
  /** The machine the agent ran on. One per run today; a field so a move can show later. */
  readonly host: string;
}

/** The tool and model an agent runs on. An empty model is the tool's own default. */
export interface AgentRuntime {
  readonly harness: Harness;
  readonly model: string;
}

/** One agent. */
export interface AgentRow extends RowBase {
  readonly kind: 'agent';
  readonly agentId: string;
  readonly parentId: string | null;
  readonly name: AgentName;
  readonly runtime: AgentRuntime;
  /** What the host says the agent's finished turns cost; null when it says nothing. */
  readonly costUsd: number | null;
  readonly ticks: readonly Tick[];
  /** How many agents this one spawned directly. */
  readonly children: number;
  /** The agents folded away under this row, in the order they would show; empty while open. */
  readonly hidden: readonly string[];
}

/** The agents of one role that one parent spawned, once there are enough to fold. */
export interface GroupRow extends RowBase {
  readonly kind: 'group';
  readonly parentId: string;
  readonly role: AgentRole;
  /** Every agent the row stands for: the members and everything under them. */
  readonly agentIds: readonly string[];
  /** The members are folded into this row. Open, they follow it as agent rows. */
  readonly folded: boolean;
  /** How many of those agents failed. */
  readonly failed: number;
  /** The distinct runtimes among every agent this folded row stands for, in first-use order. */
  readonly runtimes: readonly AgentRuntime[];
}

export type SpawnRow = AgentRow | GroupRow;

export interface SpawnStats {
  readonly total: number;
  readonly running: number;
  readonly failed: number;
  /** The most agents working at the same moment. */
  readonly peak: number;
}

export interface SpawnTree {
  readonly runId: string;
  readonly startMs: number;
  readonly endMs: number;
  readonly rows: readonly SpawnRow[];
  readonly stats: SpawnStats;
}

/**
 * What the person has chosen to fold, by row key: true folds the row, false opens it. A group
 * with no choice is folded, an agent is open.
 */
export type FoldChoices = Readonly<Record<string, boolean>>;

/** A parent's agents of one role fold into one row from this many. */
export const GROUP_FROM = 6;

export const groupKey = (parentId: string, role: AgentRole): string => `group:${parentId}:${role}`;
export const agentKey = (agentId: string): string => `agent:${agentId}`;

/** Whether a row is folded now. */
export const isFolded = (row: SpawnRow): boolean =>
  row.kind === 'group' ? row.folded : row.hidden.length > 0;

/** The choices with `row` folded or opened the other way. */
export function toggled(choices: FoldChoices, row: SpawnRow): FoldChoices {
  return {...choices, [row.key]: !isFolded(row)};
}

/** How many agents a folded row stands for; 0 for a row that is open. */
export function foldedCount(row: SpawnRow): number {
  if (row.kind === 'agent') return row.hidden.length;
  return row.folded ? row.agentIds.length : 0;
}

const STATE_OF: Readonly<Record<AgentStatus, SpawnState | 'live'>> = {
  [AgentStatus.UNSPECIFIED]: 'done',
  [AgentStatus.LAUNCHING]: 'live',
  [AgentStatus.ACTIVE]: 'live',
  [AgentStatus.WAITING]: 'live',
  [AgentStatus.IDLE]: 'done',
  [AgentStatus.ENDED]: 'done',
  [AgentStatus.FAILED]: 'failed',
  [AgentStatus.LOST]: 'failed',
};

const FINAL_RUN = new Set([RunStatus.ENDED, RunStatus.FAILED]);

interface Span {
  readonly state: SpawnState;
  readonly startMs: number;
  readonly endMs: number;
}

/** The ticks a parent shows, by parent id: one for each result a child sent back. */
function ticksByParent(
  state: OrchestratorState,
  runId: string,
  spans: ReadonlyMap<string, Span>,
): ReadonlyMap<string, Tick[]> {
  const parentOf = new Map(
    Object.values(state.agents)
      .filter(agent => agent.runId === runId && agent.parentAgentId !== '')
      .map(agent => [agent.id, agent.parentAgentId] as const),
  );
  const handedBack = new Set<string>();
  const parents = new Map<string, Tick[]>();
  const add = (childId: string, message: Message): void => {
    const parent = parentOf.get(childId);
    const atMs = ms(message.at);
    if (parent === undefined || atMs === null) return;
    const tone = spans.get(childId)?.state === 'failed' ? 'failed' : 'ok';
    parents.set(parent, [...(parents.get(parent) ?? []), {atMs, childId, tone}]);
  };
  const messages = Object.values(state.messages)
    .filter(message => message.runId === runId)
    .sort((a, b) => (ms(a.at) ?? 0) - (ms(b.at) ?? 0) || a.id.localeCompare(b.id));
  for (const message of messages) {
    const note = noteOf(message);
    if (note.kind === 'handoff' && parentOf.get(message.fromAgentId) === message.toAgentId) {
      handedBack.add(message.fromAgentId);
      add(message.fromAgentId, message);
    }
  }
  for (const message of messages) {
    const note = noteOf(message);
    if (note.kind === 'settled' && !handedBack.has(note.agentId)) add(note.agentId, message);
  }
  return parents;
}

/** Where each agent's bar starts and ends, and how it ended. */
function spansOf(
  state: OrchestratorState,
  agents: readonly Agent[],
  liveEnd: number,
  runOver: boolean,
): ReadonlyMap<string, Span> {
  const lastTurnEnd = new Map<string, number>();
  for (const turn of Object.values(state.turns)) {
    const at = ms(turn.endedAt) ?? ms(turn.startedAt);
    if (at !== null)
      lastTurnEnd.set(turn.agentId, Math.max(lastTurnEnd.get(turn.agentId) ?? at, at));
  }
  return new Map(
    agents.map(agent => {
      const startMs = ms(agent.startedAt) ?? 0;
      const kind = STATE_OF[agent.status];
      const live = kind === 'live' && !runOver;
      const endMs = live
        ? liveEnd
        : (ms(agent.endedAt) ??
          (kind === 'live' ? liveEnd : (lastTurnEnd.get(agent.id) ?? startMs)));
      const span: Span = {
        state: kind === 'live' ? (runOver ? 'done' : 'running') : kind,
        startMs,
        endMs: Math.max(startMs, endMs),
      };
      return [agent.id, span] as const;
    }),
  );
}

/** Children by parent id, in the order they started. A parent that is not in the run has none. */
function childrenOf(agents: readonly Agent[]): ReadonlyMap<string, readonly Agent[]> {
  const known = new Set(agents.map(agent => agent.id));
  const children = new Map<string, Agent[]>();
  for (const agent of agents) {
    if (agent.parentAgentId === '' || !known.has(agent.parentAgentId)) continue;
    children.set(agent.parentAgentId, [...(children.get(agent.parentAgentId) ?? []), agent]);
  }
  return children;
}

/** Marks `from` and everything reachable under it as walked. */
function markWalked(
  from: readonly Agent[],
  children: ReadonlyMap<string, readonly Agent[]>,
  seen: Set<string>,
): void {
  const stack = [...from];
  for (let agent = stack.pop(); agent !== undefined; agent = stack.pop()) {
    if (seen.has(agent.id)) continue;
    seen.add(agent.id);
    stack.push(...(children.get(agent.id) ?? []));
  }
}

/**
 * The agents to start a walk from: those with no parent in the run, then, for agents only a cycle
 * of parents reaches, the earliest one of each cycle, so every agent is walked once.
 */
function roots(agents: readonly Agent[], children: ReadonlyMap<string, readonly Agent[]>): Agent[] {
  const known = new Set(agents.map(agent => agent.id));
  const first = agents.filter(
    agent => agent.parentAgentId === '' || !known.has(agent.parentAgentId),
  );
  const seen = new Set<string>();
  markWalked(first, children, seen);
  const found = [...first];
  for (const agent of agents) {
    if (seen.has(agent.id)) continue;
    found.push(agent);
    markWalked([agent], children, seen);
  }
  return found;
}

/** The agents under `agent`, in the order a walk meets them, skipping any already in `seen`. */
function below(
  agent: Agent,
  children: ReadonlyMap<string, readonly Agent[]>,
  seen: Set<string>,
): string[] {
  const found: string[] = [];
  for (const child of children.get(agent.id) ?? []) {
    if (seen.has(child.id)) continue;
    seen.add(child.id);
    found.push(child.id, ...below(child, children, seen));
  }
  return found;
}

/** The most agents working at once. A span that only touches another does not overlap it. */
function peakOf(spans: Iterable<Span>): number {
  const edges: {at: number; delta: number}[] = [];
  for (const span of spans) {
    edges.push(
      {at: span.startMs, delta: 1},
      {at: Math.max(span.endMs, span.startMs + 1), delta: -1},
    );
  }
  edges.sort((a, b) => a.at - b.at || a.delta - b.delta);
  let live = 0;
  let peak = 0;
  for (const edge of edges) {
    live += edge.delta;
    peak = Math.max(peak, live);
  }
  return peak;
}

type Item = {readonly agent: Agent} | {readonly role: AgentRole; readonly members: Agent[]};

/** What a parent's children are drawn as: each on its own, except big groups of one role. */
function itemsOf(kids: readonly Agent[]): Item[] {
  const byRole = new Map<AgentRole, Agent[]>();
  for (const kid of kids) byRole.set(kid.role, [...(byRole.get(kid.role) ?? []), kid]);
  const placed = new Set<AgentRole>();
  const items: Item[] = [];
  for (const kid of kids) {
    const members = byRole.get(kid.role) ?? [];
    if (members.length < GROUP_FROM) items.push({agent: kid});
    else if (!placed.has(kid.role)) {
      placed.add(kid.role);
      items.push({role: kid.role, members});
    }
  }
  return items;
}

/** What a walk of the tree reads, fixed before it starts. */
interface Context {
  readonly host: string;
  readonly folds: FoldChoices;
  readonly names: ReadonlyMap<string, AgentName>;
  readonly spans: ReadonlyMap<string, Span>;
  readonly children: ReadonlyMap<string, readonly Agent[]>;
  readonly ticks: ReadonlyMap<string, readonly Tick[]>;
  readonly agents: ReadonlyMap<string, Agent>;
}

const runtimeOf = (agent: Agent): AgentRuntime => ({harness: agent.harness, model: agent.model});

/** Distinct runtimes in first-use order; equality is the contract's harness and model pair. */
function runtimesOf(agents: readonly Agent[]): AgentRuntime[] {
  const found: AgentRuntime[] = [];
  const seen = new Map<Harness, Set<string>>();
  for (const agent of agents) {
    const runtime = runtimeOf(agent);
    const models = seen.get(runtime.harness) ?? new Set<string>();
    if (models.has(runtime.model)) continue;
    models.add(runtime.model);
    seen.set(runtime.harness, models);
    found.push(runtime);
  }
  return found;
}

const lastOrMore = (index: number, count: number): Branch =>
  index === count - 1 ? 'last' : 'more';

const stateOfAll = (states: readonly SpawnState[]): SpawnState =>
  states.includes('failed') ? 'failed' : states.includes('running') ? 'running' : 'done';

/** One walk of the agents of a run, in tree order, collecting the rows and the agents they cover. */
class Walk {
  readonly rows: SpawnRow[] = [];
  readonly seen = new Set<string>();

  constructor(private readonly context: Context) {}

  private spanOf(id: string): Span {
    return this.context.spans.get(id) ?? {state: 'done', startMs: 0, endMs: 0};
  }

  private unseen(parent: Agent): Agent[] {
    return (this.context.children.get(parent.id) ?? []).filter(child => !this.seen.has(child.id));
  }

  /** Emits `agent` and, unless it is folded, everything under it. */
  agent(agent: Agent, depth: number, lineage: readonly Branch[], parentId: string | null): void {
    const {context} = this;
    this.seen.add(agent.id);
    const span = this.spanOf(agent.id);
    const kids = this.unseen(agent);
    const key = agentKey(agent.id);
    const hidden =
      kids.length > 0 && context.folds[key] === true
        ? below(agent, context.children, new Set(this.seen))
        : [];
    this.rows.push({
      kind: 'agent',
      key,
      depth,
      lineage,
      state: span.state,
      startMs: span.startMs,
      endMs: span.endMs,
      host: context.host,
      agentId: agent.id,
      parentId,
      name: context.names.get(agent.id) ?? {role: agent.role, number: null},
      runtime: runtimeOf(agent),
      costUsd: agent.usage?.costUsd ?? null,
      ticks: (context.ticks.get(agent.id) ?? []).map(tick => ({
        ...tick,
        atMs: Math.min(span.endMs, Math.max(span.startMs, tick.atMs)),
      })),
      children: kids.length,
      hidden,
    });
    if (hidden.length > 0) {
      for (const id of hidden) this.seen.add(id);
      return;
    }
    this.childrenOf(agent, depth + 1, lineage);
  }

  private group(
    parent: Agent,
    item: {readonly role: AgentRole; readonly members: readonly Agent[]},
    depth: number,
    lineage: readonly Branch[],
  ): GroupRow {
    const {context} = this;
    const local = new Set([...this.seen, ...item.members.map(member => member.id)]);
    const agentIds = item.members.flatMap(member => [
      member.id,
      ...below(member, context.children, local),
    ]);
    const covered = agentIds.map(id => this.spanOf(id));
    const key = groupKey(parent.id, item.role);
    return {
      kind: 'group',
      key,
      depth,
      lineage,
      state: stateOfAll(covered.map(span => span.state)),
      startMs: Math.min(...covered.map(span => span.startMs)),
      endMs: Math.max(...covered.map(span => span.endMs)),
      host: context.host,
      parentId: parent.id,
      role: item.role,
      agentIds,
      folded: context.folds[key] ?? true,
      failed: covered.filter(span => span.state === 'failed').length,
      runtimes: runtimesOf(
        agentIds.flatMap(id => {
          const agent = context.agents.get(id);
          return agent === undefined ? [] : [agent];
        }),
      ),
    };
  }

  /** Emits the children of `parent` at `depth`, folding big groups of one role into one row. */
  private childrenOf(parent: Agent, depth: number, above: readonly Branch[]): void {
    const items = itemsOf(this.unseen(parent));
    items.forEach((item, index) => {
      const lineage = [...above, lastOrMore(index, items.length)];
      if ('agent' in item) {
        this.agent(item.agent, depth, lineage, parent.id);
        return;
      }
      const group = this.group(parent, item, depth, lineage);
      this.rows.push(group);
      if (group.folded) {
        for (const id of group.agentIds) this.seen.add(id);
        return;
      }
      item.members.forEach((member, at) => {
        const inner = lastOrMore(at, item.members.length);
        this.agent(member, depth + 1, [...lineage, inner], parent.id);
      });
    });
  }
}

/** The spawn tree of a run, as the rows to draw. */
export function spawnTree(
  state: OrchestratorState,
  runId: string,
  options: {readonly nowMs: number; readonly host: string; readonly folds?: FoldChoices},
): SpawnTree {
  const {nowMs, host} = options;
  const run = state.runs[runId];
  const runOver = run !== undefined && FINAL_RUN.has(run.status);
  const liveEnd = runOver ? (ms(run.endedAt) ?? nowMs) : nowMs;
  const agents = startOrder(state, runId);
  const spans = spansOf(state, agents, liveEnd, runOver);
  const children = childrenOf(agents);
  const walk = new Walk({
    host,
    folds: options.folds ?? {},
    names: agentNames(state, runId),
    spans,
    children,
    ticks: ticksByParent(state, runId, spans),
    agents: new Map(agents.map(agent => [agent.id, agent] as const)),
  });
  for (const agent of roots(agents, children)) {
    if (!walk.seen.has(agent.id)) walk.agent(agent, 0, [], null);
  }
  const all = [...spans.values()];
  const start = Math.min(ms(run?.createdAt) ?? nowMs, ...all.map(span => span.startMs));
  return {
    runId,
    startMs: start,
    endMs: Math.max(liveEnd, start, ...all.map(span => span.endMs)),
    rows: walk.rows,
    stats: {
      total: agents.length,
      running: all.filter(span => span.state === 'running').length,
      failed: all.filter(span => span.state === 'failed').length,
      peak: peakOf(all),
    },
  };
}
