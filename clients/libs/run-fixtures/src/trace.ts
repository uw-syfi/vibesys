/**
 * The synthetic run builder: the span events a dynamic search writes, in the shapes the run
 * protocol carries (`span_started`, `span_updated`, `span_ended`, each with the whole span), for
 * the idea and agent-turn cases a real run produces but a recording may not hold. `Trace` numbers
 * events and stamps them on the run clock; `addIdea` writes one idea's workstream, turns and gate.
 */
import type {Span} from '@vibesys/contracts';

const START = Date.UTC(2026, 9, 15, 16, 30, 51);

const ROOT = 'flow:root#1';

type Label = NonNullable<Span['label']>;
type Attempts = NonNullable<Span['attempts']>;
type Json = Record<string, unknown>;

/** What an idea's gate or end reached. */
type IdeaEnd =
  | {readonly kind: 'running'; readonly waiting?: boolean}
  | {readonly kind: 'passed'; readonly value: number; readonly ratio: number}
  | {readonly kind: 'slower'; readonly value: number; readonly ratio: number}
  | {
      readonly kind: 'failed_check';
      readonly check: 'correctness' | 'benchmark';
      readonly completed?: number;
      readonly required?: number;
      readonly unit?: string;
    }
  | {readonly kind: 'failed'; readonly reason: string; readonly message?: string};

export interface IdeaSpec {
  readonly n: number;
  readonly title: string;
  /** The agent's own words for what it tries; defaults to none. */
  readonly idea?: string;
  /** Run-clock seconds when the idea starts. */
  readonly at: number;
  /** The idea this one builds on (its `builds_on` link and base), or null for the input. */
  readonly on?: number | null;
  /** The round that runs it, for a search that plays rounds. */
  readonly round?: number;
  /** The round of the idea it started from, null for the input. */
  readonly baseRound?: number | null;
  /** The span the workstream hangs under; the run's root by default. */
  readonly parent?: string;
  /** Reasons of earlier tries that failed before the one that settled the idea. */
  readonly tries?: readonly string[];
  readonly end: IdeaEnd;
  /** The implementer's structured outcome and summary, when it settled. */
  readonly outcome?: string;
  readonly summary?: string;
  /** How long the idea runs, in seconds; defaults to 600. */
  readonly took?: number;
}

export class Trace {
  readonly #events: Json[] = [];
  readonly #runId: string;

  constructor(runId: string) {
    this.#runId = runId;
  }

  get events(): readonly Json[] {
    return this.#events;
  }

  get last(): number {
    return this.#events.length;
  }

  /** Append an event of any type at run-clock second `at`. */
  event(type: string, data: Json | null, at: number, extra: Json = {}): void {
    const stamp = new Date(START + at * 1000).toISOString().slice(0, 19);
    this.#events.push({
      protocol_version: 1,
      run_id: this.#runId,
      sequence: this.#events.length + 1,
      timestamp: `${stamp}+00:00`,
      type,
      ...extra,
      data: data === null ? null : {kind: type, ...data},
    });
  }

  span(type: 'span_started' | 'span_updated' | 'span_ended', span: Span, at: number): void {
    this.event(type, {span}, at);
  }
}

function label(kind: string, title: string, attrs: Json, extra: Partial<Label> = {}): Label {
  return {kind, title, attrs_json: JSON.stringify(attrs), ...extra};
}

export function rootSpan(status: Span['status'], endedAt?: number): Span {
  return {
    id: ROOT,
    parent: null,
    type: 'flow',
    name: 'dynamic',
    status,
    started_at: 0,
    ...(endedAt === undefined ? {} : {ended_at: endedAt}),
  };
}

export function planningSpan(n: number, at: number, took = 40): Span {
  return {
    id: `flow:root/plan#${n}`,
    parent: ROOT,
    type: 'flow',
    name: 'plan',
    status: 'done',
    started_at: at,
    ended_at: at + took,
    label: label('planning_call', `planning call ${n}`, {call: n}),
  };
}

function ideaId(n: number): string {
  return `flow:root/ws-${n}#1`;
}

function tried(start: number, reasons: readonly string[], done: number | null): Attempts {
  const of = reasons.length + 1;
  const failed = reasons.map((reason, index) => ({
    n: index + 1,
    of,
    started_at: start + index * 30,
    ended_at: start + index * 30 + 25,
    outcome: 'again' as const,
    reason,
  }));
  return [
    ...failed,
    {
      n: of,
      of,
      started_at: start + reasons.length * 30,
      ...(done === null ? {} : {ended_at: done, outcome: 'done' as const}),
    },
  ];
}

function gateAttrs(end: IdeaEnd): Json {
  const stage = 'correctness,benchmark';
  if (end.kind === 'passed' || end.kind === 'slower') {
    return {stage, metric_value: end.value, vs_baseline: end.ratio};
  }
  if (end.kind === 'failed_check') {
    return {
      stage,
      failed: {
        check: end.check,
        completed: end.completed ?? null,
        required: end.required ?? null,
        unit: end.unit ?? null,
      },
    };
  }
  return {stage};
}

/** The verdict the gate reached, or null when the idea never reached its gate. */
function gateVerdict(end: IdeaEnd): string | null {
  if (end.kind === 'passed') return 'passed';
  if (end.kind === 'slower') return 'slower';
  if (end.kind === 'failed_check') return 'failed_check';
  return null;
}

function workstreamAttrs(spec: IdeaSpec, settled: boolean): Json {
  const base = spec.on === undefined || spec.on === null ? 'r0' : `r${spec.on}`;
  const account: Json = {};
  if (settled && spec.end.kind === 'passed') account['revision'] = `r${spec.n}`;
  if (settled && spec.outcome !== undefined) account['outcome'] = spec.outcome;
  if (settled && spec.summary !== undefined) account['summary'] = spec.summary;
  return {
    hypothesis_id: `H${spec.n}`,
    parent_revision: base,
    base_round: spec.baseRound ?? null,
    ...(spec.round === undefined ? {} : {round: spec.round}),
    ...(spec.idea === undefined ? {} : {idea: spec.idea}),
    ...account,
  };
}

function workstreamLabel(spec: IdeaSpec, settled: boolean): Label {
  const {end} = spec;
  const attrs = workstreamAttrs(spec, settled);
  const links =
    spec.on === undefined || spec.on === null ? [] : [{rel: 'builds_on', to: ideaId(spec.on)}];
  if (!settled || end.kind === 'running') return label('workstream', spec.title, attrs, {links});
  if (end.kind === 'failed') {
    return label('workstream', spec.title, attrs, {verdict: 'failed', reason: end.reason, links});
  }
  const passed = end.kind === 'passed';
  return label('workstream', spec.title, attrs, {
    verdict: passed ? 'passed' : 'failed',
    ...(passed ? {} : {reason: 'turned_down'}),
    links,
  });
}

/** Write one idea: its workstream, an implementer turn (with retries), and a gate when it got there. */
export function addIdea(trace: Trace, spec: IdeaSpec): void {
  const took = spec.took ?? 600;
  const {end} = spec;
  const running = end.kind === 'running';
  const id = ideaId(spec.n);
  const parent = spec.parent ?? ROOT;
  const stopsAt = spec.at + took;
  const workstream = (status: Span['status'], settled: boolean): Span => ({
    id,
    parent,
    type: 'flow',
    name: 'workstream',
    status,
    started_at: spec.at,
    ...(settled ? {ended_at: stopsAt} : {}),
    ...(running && end.waiting === true
      ? {waiting_on: [{kind: 'slot', ref: 'group', since: spec.at}]}
      : {}),
    label: workstreamLabel(spec, settled),
  });
  trace.span(
    'span_started',
    workstream(running && end.waiting === true ? 'waiting' : 'running', false),
    spec.at,
  );
  if (running && end.waiting === true) return;
  const tries = spec.tries ?? [];
  const turnId = `op:w${spec.n}-turn`;
  const turnStart = spec.at + 1;
  const turnEnd = turnStart + Math.floor(took * 0.6);
  const turn = (status: Span['status'], ended: boolean): Span => ({
    id: turnId,
    parent: id,
    type: 'operation',
    name: 'agent.turn',
    status,
    started_at: turnStart,
    ...(ended ? {ended_at: turnEnd} : {}),
    ...(tries.length > 0 ? {attempts: tried(turnStart, tries, ended ? turnEnd : null)} : {}),
    label: label('agent_turn', 'implementer', {
      role: 'implementer',
      model: 'example-model',
      agent: `impl-${spec.n}`,
      started_by: ['planner-1'],
    }),
  });
  trace.span('span_started', turn('running', false), turnStart);
  if (tries.length > 0)
    trace.span('span_updated', turn('running', false), turnStart + 30 * tries.length);
  if (running) return;
  if (end.kind === 'failed') {
    trace.span('span_ended', turn('failed', true), turnEnd);
    trace.span(
      'span_ended',
      {
        ...workstream('failed', true),
        failure: {kind: 'outcome', message: end.message ?? 'the idea could not be implemented'},
      },
      stopsAt,
    );
    return;
  }
  trace.span('span_ended', turn('done', true), turnEnd);
  const verdict = gateVerdict(end);
  const gate: Span = {
    id: `op:w${spec.n}-gate`,
    parent: id,
    type: 'operation',
    name: 'evaluation.measure',
    status: 'done',
    started_at: turnEnd + 1,
    ended_at: stopsAt - 1,
    label: label('gate', 'correctness + benchmark', gateAttrs(end), {
      ...(verdict === null ? {} : {verdict}),
    }),
  };
  trace.span('span_started', {...gate, status: 'running'}, turnEnd + 1);
  trace.span('span_ended', gate, stopsAt - 1);
  trace.span('span_ended', workstream(end.kind === 'passed' ? 'done' : 'done', true), stopsAt);
}

/** The run's adoption span, naming the idea that stands as the best (or the input). */
export function adoptionSpan(
  best: {readonly n: number; readonly ratio: number; readonly value: number} | null,
  at: number,
  ended: boolean,
): Span {
  return {
    id: 'op:adopt',
    parent: ROOT,
    type: 'operation',
    name: 'adopt',
    status: ended ? 'done' : 'running',
    started_at: at,
    ...(ended ? {ended_at: at + 1} : {}),
    label: label(
      'adoption',
      best === null ? 'the input' : `r${best.n}`,
      best === null
        ? {candidate: null, metric_value: 1, vs_baseline: 1}
        : {candidate: `r${best.n}`, metric_value: best.value, vs_baseline: best.ratio},
      ended ? {verdict: 'adopted'} : {},
    ),
  };
}

/** A tool-using agent turn's streamed text, as the server sends it, stamped with the turn's span. */
export function turnTranscript(
  trace: Trace,
  span: string,
  role: string,
  at: number,
  parts: readonly {readonly channel: 'analysis' | 'assistant'; readonly content: string}[],
): void {
  for (const part of parts) {
    trace.event('agent_output_chunk', {channel: part.channel, content: part.content}, at, {
      invocation_id: `inv-${span}`,
      agent_kind: role,
      span_id: span,
    });
  }
}

/** A tool call of an agent turn and its result, as the server sends them. */
export function toolCall(
  trace: Trace,
  span: string,
  role: string,
  at: number,
  call: {
    readonly id: string;
    readonly tool: string;
    readonly command: string;
    readonly output: string;
  },
): void {
  const stamp = {invocation_id: `inv-${span}`, agent_kind: role, span_id: span};
  trace.event(
    'tool_call',
    {tool: call.tool, call_id: call.id, args: {command: call.command}},
    at,
    stamp,
  );
  trace.event(
    'tool_result',
    {tool: call.tool, call_id: call.id, content: call.output, is_error: false},
    at + 1,
    stamp,
  );
}

/** What a judge's `JudgeResponse` says; a long analysis is what makes the card wide. */
export interface JudgeVerdict {
  readonly analysis: string;
  readonly verdict: 'pass' | 'fail';
  readonly feedback?: string;
}

const JUDGE_SCHEMA = {
  $defs: {Verdict: {enum: ['pass', 'fail'], type: 'string'}},
  properties: {
    analysis: {type: 'string'},
    feedback: {type: 'string'},
    verdict: {$ref: '#/$defs/Verdict'},
  },
  required: ['analysis', 'feedback', 'verdict'],
  type: 'object',
};

/**
 * A multi-judge turn of idea `n` over `[at, at + took]`: lines of thinking and then its structured
 * reply, as the server sends them (`agent_execution_finished` carries the reply).
 */
export function addJudgeTurn(
  trace: Trace,
  args: {
    readonly n: number;
    readonly at: number;
    readonly took: number;
    readonly thinking: readonly string[];
    readonly reply: JudgeVerdict;
  },
): void {
  const id = `op:w${args.n}-judge`;
  const span = (status: Span['status'], ended: boolean): Span => ({
    id,
    parent: ideaId(args.n),
    type: 'operation',
    name: 'agent.turn',
    status,
    started_at: args.at,
    ...(ended ? {ended_at: args.at + args.took} : {}),
    label: label('agent_turn', 'judge', {
      role: 'judge',
      model: 'example-model',
      agent: `judge-${args.n}`,
      started_by: ['planner-1'],
    }),
  });
  trace.span('span_started', span('running', false), args.at);
  const stamp = {invocation_id: `inv-${id}`, agent_kind: 'multi-judge', span_id: id};
  for (const line of args.thinking) {
    trace.event('agent_output_chunk', {channel: 'analysis', content: line}, args.at + 1, stamp);
  }
  const value = {
    analysis: args.reply.analysis,
    feedback: args.reply.feedback ?? '',
    verdict: args.reply.verdict,
    skills_used: [],
  };
  trace.event(
    'agent_execution_finished',
    {
      result: JSON.stringify({reply: value}),
      reply: {kind: 'JudgeResponse', json_schema: JUDGE_SCHEMA, value},
    },
    args.at + args.took - 1,
    {status: 'completed', ...stamp},
  );
  trace.span('span_ended', span('done', true), args.at + args.took);
}
