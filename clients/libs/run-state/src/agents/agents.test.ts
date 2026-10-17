import {describe, expect, it} from 'bun:test';
import type {RunEvent, RunSnapshot, Span} from '@vibesys/contracts';
import {EMPTY_TRACE, foldTrace, traceSpans} from '../trace/index.js';
import {
  AgentFold,
  type AgentGraph,
  type AgentNode,
  type AgentWithin,
  agentGraph,
  agentsStartedBy,
  type WorkMeter,
} from './index.js';

type SpanStatus = Span['status'];

/** mulberry32: a seeded generator whose low bits vary (fast-check is not a dependency). */
class Seeded {
  #state: number;

  constructor(seed: number) {
    this.#state = seed >>> 0;
  }

  int(bound: number): number {
    this.#state = (this.#state + 0x6d2b79f5) >>> 0;
    let value = this.#state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) % bound;
  }

  pick<T>(items: readonly T[]): T {
    return items[this.int(items.length)] as T;
  }
}

const ROOT = 'flow:#1';
const ENDED: readonly SpanStatus[] = ['done', 'failed', 'stopped'];
const LIVE: readonly SpanStatus[] = ['running', 'waiting', 'pending'];

function event(sequence: number, span: Span, first: boolean): RunEvent {
  const ended = ENDED.includes(span.status);
  const type = first ? 'span_started' : ended ? 'span_ended' : 'span_updated';
  return {
    protocol_version: 1,
    run_id: 'run',
    sequence,
    timestamp: '2026-01-01T00:00:00Z',
    type,
    data: {kind: type, span},
  } as RunEvent;
}

/** The events of span versions in the order given: a span's first version starts it. */
function eventsIn(order: readonly Span[]): RunEvent[] {
  const seen = new Set<string>();
  return order.map((span, index) => {
    const first = !seen.has(span.id);
    seen.add(span.id);
    return event(index + 1, span, first);
  });
}

/**
 * Each span's versions in a random interleaving: spans start in order (as a run starts them,
 * every parent before its children) and their later versions come at any time after.
 */
function shuffled(random: Seeded, versions: readonly Span[][]): RunEvent[] {
  const pending = versions.map(list => [...list]);
  let next = 0;
  const order: Span[] = [];
  while (pending.some(list => list.length > 0)) {
    const started = pending.slice(0, next).filter(list => list.length > 0);
    const choices = next < pending.length ? [...started, pending[next] as Span[]] : started;
    const list = random.pick(choices);
    if (list === pending[next]) next += 1;
    order.push(list.shift() as Span);
  }
  return eventsIn(order);
}

function folded(events: readonly RunEvent[]): Span[] {
  return [...traceSpans(foldTrace(EMPTY_TRACE, events))];
}

const root: Span = {
  id: ROOT,
  parent: null,
  type: 'flow',
  name: 'search',
  status: 'running',
  started_at: 0,
};

function labelled(id: string, parent: string, at: number, kind: string, title: string): Span {
  return {
    id,
    parent,
    type: 'flow',
    name: kind,
    status: 'running',
    started_at: at,
    label: {kind, title},
  };
}

function task(id: string, parent: string, at: number): Span {
  return {id, parent, type: 'flow', name: 'task', status: 'running', started_at: at};
}

interface TurnOf {
  readonly agent: string;
  readonly role: string;
  readonly startedBy?: readonly string[];
  readonly model?: string | null;
}

function turn(id: string, parent: string, at: number, of: TurnOf, status: SpanStatus): Span {
  const attrs = {
    agent: of.agent,
    role: of.role,
    model: of.model ?? null,
    started_by: of.startedBy ?? [],
  };
  return {
    id,
    parent,
    type: 'operation',
    name: 'turn',
    status,
    started_at: at,
    ended_at: ENDED.includes(status) ? at + 0.25 : null,
    label: {kind: 'agent_turn', title: of.role, attrs_json: JSON.stringify(attrs)},
  };
}

/** A generated agent: its identity, role and starters among the agents before it. */
function generatedAgent(random: Seeded, n: number, earlier: readonly string[]): TurnOf {
  const startedBy = new Set<string>();
  for (let k = random.int(3); k > 0 && earlier.length > 0; k -= 1) {
    startedBy.add(random.pick(earlier));
  }
  const role = random.pick(['planner', 'implementer', 'judge', 'profiler']);
  return {agent: `session:${role}:a${n}`, role, startedBy: [...startedBy]};
}

/** Where a turn runs: under a labelled span, an unlabelled task, a span not started, or the root. */
function generatedParent(random: Seeded, spans: readonly string[]): string {
  return random.int(8) === 0 ? 'flow:never/#1' : random.pick(spans);
}

/** One turn's versions: it may start unlabelled, end, or move to another agent on a later update. */
function generatedTurn(
  random: Seeded,
  id: string,
  parent: string,
  at: number,
  agents: readonly TurnOf[],
): Span[] {
  const of = random.pick(agents);
  const begun = turn(id, parent, at, of, 'running');
  const versions = [random.int(4) === 0 ? {...begun, label: null} : begun];
  for (let k = random.int(3); k > 0; k -= 1) {
    versions.push(turn(id, parent, at, of, random.pick(LIVE)));
  }
  const moved = random.int(10) === 0 ? random.pick(agents) : of;
  versions.push(turn(id, parent, at, moved, random.pick([...LIVE, ...ENDED])));
  if (random.int(12) === 0) versions.push({...begun, label: null});
  return versions;
}

/**
 * A generated run, as each span's versions: labelled spans (some labelled only later, some
 * retitled), unlabelled tasks under them, and agents taking turns under any of them, each agent
 * started by none, one or several earlier agents.
 */
function generated(random: Seeded): Span[][] {
  const versions: Span[][] = [[root]];
  const parents: string[] = [ROOT];
  for (let n = 0; n < 1 + random.int(4); n += 1) {
    const id = `flow:w${n}/#1`;
    const kind = random.pick(['workstream', 'planning_call']);
    const span = labelled(id, random.pick(parents), n, kind, `work ${n}`);
    const late = random.int(3) === 0;
    versions.push(late ? [{...span, label: null}, span] : [span]);
    if (random.int(3) === 0) versions.push([{...span, label: {kind, title: `work ${n} again`}}]);
    parents.push(id);
    if (random.int(2) === 0) {
      versions.push([task(`flow:w${n}/task/#1`, id, n + 0.5)]);
      parents.push(`flow:w${n}/task/#1`);
    }
  }
  const agents: TurnOf[] = [];
  for (let n = 0; n < 1 + random.int(5); n += 1) {
    agents.push(
      generatedAgent(
        random,
        n,
        agents.map(a => a.agent),
      ),
    );
  }
  for (let t = 0; t < random.int(12); t += 1) {
    const at = 10 + t;
    const id = `op:turn${t}`;
    versions.push(generatedTurn(random, id, generatedParent(random, parents), at, agents));
  }
  return versions;
}

/** The agent graph of these spans, built directly from its definition: the oracle. */
function expected(spans: readonly Span[]): AgentGraph {
  const byId = new Map(spans.map(span => [span.id, span]));
  const turns = new Map<string, Span[]>();
  for (const span of spans) {
    if (span.label?.kind !== 'agent_turn') continue;
    const agent = JSON.parse(span.label.attrs_json ?? '{}')['agent'];
    if (typeof agent !== 'string' || agent === '') continue;
    turns.set(agent, [...(turns.get(agent) ?? []), span]);
  }
  const agents = [...turns.entries()].map(([id, list]): AgentNode => {
    const first = list[0] as Span;
    const latest = list.at(-1) as Span;
    const attrs = JSON.parse(latest.label?.attrs_json ?? '{}');
    return {
      id,
      role: attrs['role'],
      model: attrs['model'],
      startedBy: attrs['started_by'],
      status: latest.status,
      activity: {
        turn: latest.id,
        status: latest.status,
        startedAt: latest.started_at,
        endedAt: latest.ended_at ?? null,
        within: withinOf(latest, byId),
      },
      turns: list.length,
      startedAt: first.started_at,
    };
  });
  return {agents};
}

function withinOf(span: Span, byId: ReadonlyMap<string, Span>): AgentWithin | null {
  let at = span.parent ? byId.get(span.parent) : undefined;
  while (at !== undefined && !at.label?.kind) at = at.parent ? byId.get(at.parent) : undefined;
  if (at?.label === undefined || at.label === null) return null;
  return {span: at.id, kind: at.label.kind, title: at.label.title || at.name};
}

/** Counts the fold's work: a Fake meter. */
class CountingMeter implements WorkMeter {
  units = 0;

  count(units: number): void {
    this.units += units;
  }
}

const WORKSTREAMS = 20;
const ROLES = ['implementer', 'judge', 'profiler'] as const;
const TURNS = 40;
const TURN_UPDATES = 39;
const RETITLES = 10;
const PLANNER: TurnOf = {agent: 'session:orchestrator:plan', role: 'orchestrator'};

function agentOf(w: number, role: string): TurnOf {
  const implementer = `session:implementer:w${w}`;
  const startedBy = role === 'judge' ? [implementer] : [PLANNER.agent];
  return {agent: `session:${role}:w${w}`, role, startedBy};
}

/** One agent's turns, one after another, each updated often while it runs. */
function turnsOf(of: TurnOf, parent: string, tag: string): Span[] {
  const versions: Span[] = [];
  for (let t = 0; t < TURNS; t += 1) {
    const id = `op:${tag}/turn${t}`;
    const at = t;
    for (let u = 0; u <= TURN_UPDATES; u += 1) versions.push(turn(id, parent, at, of, 'running'));
    versions.push(turn(id, parent, at, of, 'done'));
  }
  return versions;
}

/** Span versions merged one from each list in turn, each list kept in order. */
function roundRobin(lists: readonly Span[][]): Span[] {
  const order: Span[] = [];
  const queues = lists.map(list => [...list]);
  for (let index = 0; queues.some(queue => queue.length > 0); index = (index + 1) % queues.length) {
    const next = queues[index]?.shift();
    if (next !== undefined) order.push(next);
  }
  return order;
}

interface LongRun {
  readonly events: RunEvent[];
  /** The agents whose node a change to each span can change. */
  readonly affects: ReadonlyMap<string, readonly string[]>;
}

/**
 * A long run: a planner plans twenty workstreams, each with an implementer, the judge it starts
 * and a profiler; every agent takes forty turns, each updated forty times, all interleaved; the
 * workstreams are retitled now and then.
 */
function longRun(): LongRun {
  const plan = labelled('flow:plan/#1', ROOT, 0, 'planning_call', 'plan');
  const order: Span[] = [root, plan];
  const lists: Span[][] = [turnsOf(PLANNER, plan.id, 'plan')];
  const affects = new Map<string, string[]>([[plan.id, [PLANNER.agent]]]);
  for (let w = 0; w < WORKSTREAMS; w += 1) {
    const ws = labelled(`flow:w${w}/#1`, ROOT, w, 'workstream', `workstream ${w}`);
    order.push(ws);
    affects.set(
      ws.id,
      ROLES.map(role => agentOf(w, role).agent),
    );
    for (const role of ROLES) lists.push(turnsOf(agentOf(w, role), ws.id, `w${w}/${role}`));
    lists.push(
      Array.from({length: RETITLES}, (_, k) => ({
        ...ws,
        label: {kind: 'workstream', title: `workstream ${w}, try ${k}`},
      })),
    );
  }
  order.push(...roundRobin(lists));
  for (const span of order) {
    if (span.label?.kind !== 'agent_turn') continue;
    affects.set(span.id, [JSON.parse(span.label.attrs_json ?? '{}')['agent']]);
  }
  return {events: eventsIn(order), affects};
}

function spanIdOf(event: RunEvent): string {
  const data = event.data as {span?: Span} | null;
  return data?.span?.id ?? '';
}

describe('agent graph', () => {
  it('is the graph of the spans folded so far, one batch of events at a time', () => {
    for (let seed = 1; seed <= 400; seed += 1) {
      const random = new Seeded(seed);
      const events = shuffled(random, generated(random));
      const fold = new AgentFold();
      let end = 0;
      while (end < events.length) {
        const next = Math.min(events.length, end + 1 + random.int(4));
        fold.apply(events.slice(end, next));
        end = next;
        const spans = folded(events.slice(0, end));
        expect(fold.graph()).toEqual(expected(spans));
        expect(agentGraph(spans)).toEqual(expected(spans));
      }
    }
  });

  it('names as starters only agents, never the agent itself, when the run says so', () => {
    for (let seed = 1; seed <= 200; seed += 1) {
      const random = new Seeded(seed);
      const graph = agentGraph(folded(shuffled(random, generated(random))));
      const ids = graph.agents.map(agent => agent.id);
      for (const agent of graph.agents) {
        expect(agent.startedBy).not.toContain(agent.id);
        expect(agent.turns).toBeGreaterThan(0);
        for (const starter of agent.startedBy) {
          expect(agentsStartedBy(graph, starter)).toContainEqual(agent);
        }
      }
      expect(new Set(ids).size).toBe(ids.length);
    }
  });

  it('skips the events a snapshot already holds', () => {
    for (let seed = 1; seed <= 50; seed += 1) {
      const random = new Seeded(seed);
      const events = shuffled(random, generated(random));
      const cut = random.int(events.length + 1);
      const snapshot = {sequence: cut, spans: folded(events.slice(0, cut))} as RunSnapshot;
      const fold = AgentFold.fromSnapshot(snapshot);
      fold.apply(events);
      expect(fold.graph()).toEqual(expected(folded(events)));
    }
  });

  it('folds a long run in constant work per event, reading once per batch', () => {
    const {events, affects} = longRun();
    const meter = new CountingMeter();
    const fold = new AgentFold(meter);
    let applied = 0;
    let bound = 0;
    for (let start = 0; start < events.length; start += 500) {
      const slice = events.slice(start, start + 500);
      const before = meter.units;
      fold.apply(slice);
      applied += meter.units - before;
      // an event costs its span, plus a mark for each agent working under that span
      bound += slice.reduce((sum, e) => sum + 1 + (affects.get(spanIdOf(e))?.length ?? 0), 0);
      const touched = new Set(slice.flatMap(e => affects.get(spanIdOf(e)) ?? []));
      const reading = meter.units;
      const graph = fold.graph();
      // a read builds again only the agents this batch touched, plus one pass over them all
      expect(meter.units - reading).toBeLessThanOrEqual(graph.agents.length + 2 * touched.size);
      const again = meter.units;
      expect(fold.graph()).toBe(graph);
      expect(meter.units).toBe(again);
    }
    expect(events.length).toBeGreaterThan(100_000);
    expect(applied).toBeLessThanOrEqual(bound);
    // no span has more than a workstream's three agents under it, however long the run
    expect(bound).toBeLessThanOrEqual((1 + ROLES.length) * events.length);

    const graph = fold.graph();
    expect(graph).toEqual(expected(folded(events)));
    expect(graph.agents).toHaveLength(1 + WORKSTREAMS * ROLES.length);
    expect(graph.agents.every(agent => agent.turns === TURNS && agent.status === 'done')).toBe(
      true,
    );
    expect(agentsStartedBy(graph, PLANNER.agent)).toHaveLength(2 * WORKSTREAMS);
    const judge = graph.agents.find(agent => agent.id === 'session:judge:w7');
    expect(judge?.startedBy).toEqual(['session:implementer:w7']);
    expect(judge?.activity.within).toEqual({
      span: 'flow:w7/#1',
      kind: 'workstream',
      title: `workstream 7, try ${RETITLES - 1}`,
    });
  });

  it('shows what each agent does now, who started it, and how many turns it took', () => {
    const plan = labelled('flow:plan/#1', ROOT, 1, 'planning_call', 'plan round 1');
    const ws = labelled('flow:w/#1', ROOT, 2, 'workstream', 'fuse kernels');
    const inner = task('flow:w/build/#1', ws.id, 2.1);
    const planner: TurnOf = {agent: 'session:orchestrator:r1', role: 'orchestrator'};
    const other: TurnOf = {agent: 'session:orchestrator:r0', role: 'orchestrator'};
    const builder: TurnOf = {
      agent: 'session:implementer:w',
      role: 'implementer',
      model: 'opus',
      startedBy: [other.agent, planner.agent],
    };
    const graph = agentGraph([
      root,
      turn('op:r0', ROOT, 0.5, other, 'done'),
      plan,
      turn('op:p1', plan.id, 1.1, planner, 'done'),
      ws,
      inner,
      turn('op:b1', inner.id, 2.2, builder, 'done'),
      turn('op:b2', inner.id, 2.3, builder, 'running'),
    ]);

    expect(graph.agents.map(agent => [agent.id, agent.turns, agent.status])).toEqual([
      [other.agent, 1, 'done'],
      [planner.agent, 1, 'done'],
      [builder.agent, 2, 'running'],
    ]);
    const built = graph.agents[2];
    expect(built?.startedBy).toEqual([other.agent, planner.agent]);
    expect([built?.role, built?.model, built?.startedAt]).toEqual(['implementer', 'opus', 2.2]);
    expect(built?.activity).toEqual({
      turn: 'op:b2',
      status: 'running',
      startedAt: 2.3,
      endedAt: null,
      within: {span: ws.id, kind: 'workstream', title: 'fuse kernels'},
    });
    expect(graph.agents[0]?.activity.within).toBeNull();
    expect(agentsStartedBy(graph, planner.agent).map(agent => agent.id)).toEqual([builder.agent]);
  });

  it('finds a turn its labelled ancestor when the label or the ancestor comes after the turn', () => {
    const of: TurnOf = {agent: 'session:implementer:w', role: 'implementer'};
    const ws = labelled('flow:w/#1', ROOT, 1, 'workstream', 'cache');
    const inner = task('flow:w/t/#1', ws.id, 1.1);
    const fold = new AgentFold();
    fold.applySpans([root, {...ws, label: null}, turn('op:t', inner.id, 1.2, of, 'running')]);
    expect(fold.graph().agents[0]?.activity.within).toBeNull();
    fold.applySpans([inner]);
    expect(fold.graph().agents[0]?.activity.within).toBeNull();
    fold.applySpans([ws]);
    expect(fold.graph().agents[0]?.activity.within?.title).toBe('cache');
    fold.applySpans([{...inner, label: {kind: 'gate', title: 'measure'}}]);
    expect(fold.graph().agents[0]?.activity.within).toEqual({
      span: inner.id,
      kind: 'gate',
      title: 'measure',
    });
  });
});
