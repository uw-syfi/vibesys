/**
 * The run's agents and who started whom, folded from the run's span events. An agent is one
 * conversation with one role; it may be started by several agents (fan-in), and the search
 * itself starts the agents that name none.
 *
 * It reads only the `agent_turn` spans every search labels (`vibesys.search_spans`): each turn
 * names its `agent` (stable across replay and restart), its `role` and `model`, and the agents
 * that started it (`started_by`). An agent's status and activity are its latest turn's; the span
 * its activity runs `within` is the turn's nearest labelled ancestor (a workstream, a planning
 * call), whose title says what the agent works on.
 *
 * Folding costs constant work per event: the fold keeps, per agent, its turns and a cached node
 * that an event only marks stale (a turn's agent, or the agents whose latest turn runs under the
 * span, up to its nearest labelled ancestor). A read builds the stale nodes again, plus one pass
 * over the agents, so a client reads once per batch of events.
 */
import type {RunEvent, RunSnapshot, Span} from '@vibesys/contracts';

type SpanStatus = Span['status'];

/** What an agent does now: its latest turn, and the labelled span that turn runs within. */
export interface AgentActivity {
  /** The latest turn's span. */
  readonly turn: string;
  readonly status: SpanStatus;
  readonly startedAt: number;
  readonly endedAt: number | null;
  /** The turn's nearest labelled ancestor: what the agent works on; null when there is none. */
  readonly within: AgentWithin | null;
}

/** A labelled span an agent's turn runs within. */
export interface AgentWithin {
  readonly span: string;
  /** The label's kind: `workstream`, `planning_call`, … */
  readonly kind: string;
  /** The label's title, else the span's name. */
  readonly title: string;
}

/** One agent. */
export interface AgentNode {
  /** Stable across replay and restart. */
  readonly id: string;
  readonly role: string;
  /** The model it runs on; null for the run's default. */
  readonly model: string | null;
  /** The agents whose work started it, in the order its turn names them; empty: the search did. */
  readonly startedBy: readonly string[];
  /** Its latest turn's status. */
  readonly status: SpanStatus;
  readonly activity: AgentActivity;
  /** How many turns it has taken (or is taking). */
  readonly turns: number;
  /** When its first turn started, on the run's clock. */
  readonly startedAt: number;
}

/** The run's agents, in the order their first turns started. */
export interface AgentGraph {
  readonly agents: readonly AgentNode[];
}

/** Counts the fold's work, in spans and nodes visited; tests bound it per event. */
export interface WorkMeter {
  count(units: number): void;
}

interface AgentState {
  readonly id: string;
  /** Its turns' spans, by the position each first came in. */
  readonly turns: Map<string, number>;
  first: Span;
  latest: Span;
  /** The ids above its latest turn up to the nearest labelled span, whose change can change it. */
  watching: readonly string[];
  view: AgentNode | null;
}

const SPAN_EVENTS: ReadonlySet<string> = new Set(['span_started', 'span_updated', 'span_ended']);
const NO_METER: WorkMeter = {count: () => undefined};

/** The incremental fold of the run's span events into its agent graph. */
export class AgentFold {
  readonly #meter: WorkMeter;
  #sequence = 0;
  readonly #spans = new Map<string, Span>();
  readonly #order = new Map<string, number>();
  readonly #agents = new Map<string, AgentState>();
  readonly #agentOfTurn = new Map<string, string>();
  readonly #watchers = new Map<string, Set<string>>();
  #graph: AgentGraph | null = null;

  constructor(meter: WorkMeter = NO_METER) {
    this.#meter = meter;
  }

  /** A fold that starts from a snapshot's spans, at the snapshot's sequence. */
  static fromSnapshot(snapshot: RunSnapshot, meter?: WorkMeter): AgentFold {
    const fold = new AgentFold(meter);
    fold.applySpans(snapshot.spans ?? []);
    fold.#sequence = snapshot.sequence;
    return fold;
  }

  /** Folds `events` in order; one at or before the last folded sequence is already in. */
  apply(events: Iterable<RunEvent>): void {
    for (const event of events) {
      const at = event.sequence ?? 0;
      if (at <= this.#sequence) continue;
      this.#sequence = at;
      const span = spanOf(event);
      if (span !== null) this.#put(span);
    }
  }

  /** Folds whole spans in the order they started, each replacing the span of its id. */
  applySpans(spans: Iterable<Span>): void {
    for (const span of spans) this.#put(span);
  }

  /** The agent graph now; the same object until a span changes. */
  graph(): AgentGraph {
    if (this.#graph !== null) return this.#graph;
    this.#meter.count(this.#agents.size);
    const states = [...this.#agents.values()];
    // agents come in as their first turns do; one sorts out of place only when a turn moved
    states.sort((a, b) => this.#at(a.first) - this.#at(b.first));
    this.#graph = {agents: states.map(state => this.#node(state))};
    return this.#graph;
  }

  #put(span: Span): void {
    this.#meter.count(1);
    if (!this.#order.has(span.id)) this.#order.set(span.id, this.#order.size);
    this.#spans.set(span.id, span);
    this.#graph = null;
    for (const agent of this.#watchers.get(span.id) ?? []) {
      this.#meter.count(1);
      const state = this.#agents.get(agent);
      if (state !== undefined) state.view = null;
    }
    const agent =
      span.label?.kind === 'agent_turn' ? stringOrNull(attributes(span)['agent']) : null;
    const known = this.#agentOfTurn.get(span.id);
    if (known !== undefined && known !== agent) this.#forget(known, span.id);
    if (agent !== null) this.#turn(agent, span);
  }

  #turn(id: string, span: Span): void {
    this.#agentOfTurn.set(span.id, id);
    const at = this.#at(span);
    const state = this.#agents.get(id);
    if (state === undefined) {
      const turns = new Map([[span.id, at]]);
      this.#agents.set(id, {id, turns, first: span, latest: span, watching: [], view: null});
      return;
    }
    state.turns.set(span.id, at);
    if (at <= this.#at(state.first)) state.first = span;
    if (at >= this.#at(state.latest)) state.latest = span;
    state.view = null;
  }

  /** The span `turn` is no longer a turn of `agent`: its first and latest turns are found again. */
  #forget(agent: string, turn: string): void {
    this.#agentOfTurn.delete(turn);
    const state = this.#agents.get(agent);
    if (state === undefined) return;
    state.turns.delete(turn);
    this.#meter.count(state.turns.size);
    this.#unwatch(state);
    state.view = null;
    const ids = [...state.turns.keys()];
    const first = ids[0];
    if (first === undefined) {
      this.#agents.delete(agent);
      return;
    }
    let low = first;
    let high = first;
    for (const id of ids) {
      if ((state.turns.get(id) ?? 0) < (state.turns.get(low) ?? 0)) low = id;
      if ((state.turns.get(id) ?? 0) > (state.turns.get(high) ?? 0)) high = id;
    }
    state.first = this.#spans.get(low) ?? state.first;
    state.latest = this.#spans.get(high) ?? state.latest;
  }

  #node(state: AgentState): AgentNode {
    if (state.view !== null) return state.view;
    const latest = state.latest;
    const within = this.#watch(state);
    this.#meter.count(state.watching.length + 1);
    const attrs = attributes(latest);
    state.view = {
      id: state.id,
      role: stringOrNull(attrs['role']) ?? (latest.label?.title || latest.name),
      model: stringOrNull(attrs['model']),
      startedBy: strings(attrs['started_by']),
      status: latest.status,
      activity: {
        turn: latest.id,
        status: latest.status,
        startedAt: latest.started_at,
        endedAt: latest.ended_at ?? null,
        within,
      },
      turns: state.turns.size,
      startedAt: state.first.started_at,
    };
    return state.view;
  }

  /**
   * Watches the ids above the agent's latest turn, up to its nearest labelled ancestor (or one not
   * folded yet), so a change to any of them marks the agent stale; returns that labelled ancestor.
   */
  #watch(state: AgentState): AgentWithin | null {
    this.#unwatch(state);
    const above: string[] = [];
    let found: Span | null = null;
    for (let id = state.latest.parent; id; ) {
      above.push(id);
      const watchers = this.#watchers.get(id);
      if (watchers === undefined) this.#watchers.set(id, new Set([state.id]));
      else watchers.add(state.id);
      const span = this.#spans.get(id);
      if (span?.label?.kind) found = span;
      if (span === undefined || found !== null) break;
      id = span.parent;
    }
    state.watching = above;
    const kind = found?.label?.kind;
    if (found === null || !kind) return null;
    return {span: found.id, kind, title: found.label?.title || found.name};
  }

  #unwatch(state: AgentState): void {
    for (const id of state.watching) {
      const watchers = this.#watchers.get(id);
      watchers?.delete(state.id);
      if (watchers?.size === 0) this.#watchers.delete(id);
    }
    state.watching = [];
  }

  #at(span: Span): number {
    return this.#order.get(span.id) ?? 0;
  }
}

/** The agent graph of the run whose spans these are, in the order they started. */
export function agentGraph(spans: readonly Span[]): AgentGraph {
  const fold = new AgentFold();
  fold.applySpans(spans);
  return fold.graph();
}

/** The agents `agent` started, in the order their first turns started. */
export function agentsStartedBy(graph: AgentGraph, agent: string): AgentNode[] {
  return graph.agents.filter(item => item.startedBy.includes(agent));
}

function attributes(span: Span): Record<string, unknown> {
  const text = span.label?.attrs_json;
  if (!text) return {};
  const parsed: unknown = JSON.parse(text);
  return typeof parsed === 'object' && parsed !== null ? (parsed as Record<string, unknown>) : {};
}

function stringOrNull(value: unknown): string | null {
  return typeof value === 'string' && value !== '' ? value : null;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter(item => typeof item === 'string' && item !== '') : [];
}

function spanOf(event: RunEvent): Span | null {
  if (!SPAN_EVENTS.has(event.type)) return null;
  const data = event.data;
  return data !== null && data !== undefined && 'span' in data ? data.span : null;
}
