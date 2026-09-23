/** Pure derivations from store state to the view models in `model.ts`. No React, no DOM. */
import {Graph, layout} from '@dagrejs/dagre';
import type {
  DesignRound,
  HypothesisEntry,
  HypothesisRound,
  PerformanceContext,
  PerformanceRound,
  ProtocolResponse,
  RunEvent,
} from '@vibesys/backend-client/browser';
import {
  type AgentPhase,
  type CoreRunStatus,
  type CoreState,
  hasRunEnded,
  phasesForRound,
  type RoundState,
  roundNumberFromLabel,
  type TranscriptEntry,
} from '@vibesys/core-state';
import type {
  AgentGraph,
  Connection,
  ConsumedSteer,
  EdgeTone,
  EndedWord,
  GraphEdge,
  GraphLayout,
  GraphNode,
  HeaderModel,
  InspectorModel,
  JudgeAttempt,
  LogGroup,
  LogItem,
  PendingSteer,
  ProsePart,
  RailModel,
  RailRow,
  RailState,
  RoundStatus,
  RunControl,
  RunPulse,
  Steers,
  ToolResultSummary,
  TrendModel,
  Verdict,
} from './model.js';

const compact = new Intl.NumberFormat('en', {notation: 'compact', maximumSignificantDigits: 4});
const exact = new Intl.NumberFormat('en', {maximumFractionDigits: 3});

export function formatValue(value: number): string {
  return compact.format(value);
}

export function formatDuration(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor(total / 60) % 60;
  const seconds = String(total % 60).padStart(2, '0');
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, '0')}:${seconds}`
    : `${minutes}:${seconds}`;
}

export function formatDelta(pct: number): string {
  return `${pct > 0 ? '+' : ''}${pct.toFixed(1)}%`;
}

/** The latest round the run has started, or null before round 1. */
export function latestRound(core: CoreState): number | null {
  return core.rounds.filter(round => round.status !== 'planned').at(-1)?.number ?? null;
}

/** `perf_eval` as `Perf eval`: how a role or a harness is written as a word. */
export function titleCase(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1).replaceAll('_', ' ');
}

const HARNESSES: Record<string, string> = {
  codex: 'Codex',
  claude: 'Claude Code',
  gemini: 'Gemini',
  opencode: 'Opencode',
};

/**
 * What an agent ran on. The card has room for one of the two, so it shows the model id
 * (`claude-opus-5`), which is the fact that varies between agents; the harness rarely does. When
 * both are recorded, `tip` pairs them (`Claude Code (claude-opus-5)`) for the tooltip and the
 * accessible name. A phase with no model shows the harness instead, and one with neither shows
 * no runtime line at all.
 *
 * ponytail: the TUI's `agent-runtime-label.ts` also prettifies the model id (`GPT 5.1 Codex Max`)
 * from a table of acronyms. That table has to learn every new model, so this keeps the recorded
 * id and the two labels differ by design.
 */
function runtimeLabel(
  provider: string | null | undefined,
  model: string | null | undefined,
): {runtime: string | null; tip: string | null} {
  const recorded = provider?.trim();
  const harness = recorded ? (HARNESSES[recorded.toLowerCase()] ?? titleCase(recorded)) : null;
  const name = model?.trim() || null;
  if (name === null) return {runtime: harness, tip: null};
  if (harness === null) return {runtime: name, tip: null};
  return {runtime: name, tip: `${harness} (${name})`};
}

const BROKEN = new Set<AgentPhase['status']>(['failed', 'cancelled', 'interrupted']);

/**
 * The arrow between two agents. A failure at the source outranks everything: it is why the
 * handover never happened. Otherwise an active agent at either end makes the arrow the live one,
 * and a source that has finished has handed over.
 */
function edgeTone(from: GraphNode, to: GraphNode): EdgeTone {
  if (BROKEN.has(from.status)) return 'failed';
  if (from.status === 'active' || to.status === 'active') return 'live';
  if (from.status === 'completed') return 'done';
  return 'idle';
}

/**
 * One kind's cards. Adjacent agents of the kind that share a status and a runtime say the same
 * thing, so they become one card with a count; the loop running the orchestrator twice a round is
 * the common case. Agents that differ in either stay separate cards, and only adjacent ones
 * collapse, so a failure between two successes keeps all three.
 */
function graphNodes(phases: readonly AgentPhase[], kind: string): GraphNode[] {
  const nodes: GraphNode[] = [];
  for (const [row, phase] of phases.entries()) {
    const {runtime, tip} = runtimeLabel(phase.provider, phase.model);
    const open = nodes.at(-1);
    // Both halves, so one model run on two harnesses stays two cards.
    if (
      open !== undefined &&
      open.status === phase.status &&
      open.runtime === runtime &&
      open.runtimeTip === tip
    ) {
      open.count += 1;
      continue;
    }
    nodes.push({
      id: phase.executionId ?? `${kind}-${row}`,
      role: titleCase(kind),
      status: phase.status,
      runtime,
      runtimeTip: tip,
      count: 1,
    });
  }
  return nodes;
}

/**
 * The handovers of a round whose events carry none: each card hands over to the next in the order
 * they ran. Within a kind that is its retries in sequence, and between kinds it is the last card of
 * one and the first of the next, so no handover is drawn that did not happen. Wiring every card of
 * a kind to every card of the next would invent one: a retried orchestrator would claim two
 * handovers into the implementer where the loop made one. This is the fallback until the backend
 * reports edges; that day it is replaced by reading them and nothing else in the graph changes.
 */
function inferChainEdges(nodes: readonly GraphNode[]): GraphEdge[] {
  const edges: GraphEdge[] = [];
  for (const [index, to] of nodes.entries()) {
    const from = nodes[index - 1];
    if (from !== undefined) edges.push({from: from.id, to: to.id, tone: edgeTone(from, to)});
  }
  return edges;
}

/**
 * The round's agents and the handovers between them: the agents of a kind collapsed into cards, in
 * the order the round first mentions the kind, and one edge per handover. Nodes stay in loop order
 * whatever the layout later does with them.
 *
 * Roles the loop has not reached are already in `core.phases` as pending: run-map seeds them from
 * the `expected_roles` that `run_started` advertised, so nothing is re-derived here. A round with
 * no phases (R0, or one the run has not started) yields nothing and the panel does not render.
 */
export function agentGraph(core: CoreState, round: number | null): AgentGraph {
  if (round === null) return {nodes: [], edges: []};
  const phases = phasesForRound(core.phases, round);
  const nodes = [...new Set(phases.map(phase => phase.kind))].flatMap(kind =>
    graphNodes(
      phases.filter(phase => phase.kind === kind),
      kind,
    ),
  );
  return {nodes, edges: inferChainEdges(nodes)};
}

/**
 * The card's box. The study of comparable tools puts a pipeline node at roughly 150x40 (GitHub
 * Actions' 292x100 is the outlier), and four ranks of 160 with a 32 px gap are 736 px, which is
 * the widest a round can be and still fit the 744 px measure at 1440 without scrolling.
 */
export const NODE = {width: 160, height: 44};
const RANK_GAP = 32;
/** The gap between stacked cards, which is also what the panel's three-row cap is counted in. */
export const NODE_GAP = 12;

/**
 * Where the cards and the arrows go: left to right, a rank per hop, ranks tight enough that the
 * common four-agent round needs no scrollbar. Dagster's ranker, for the reason Dagster gives
 * (`tight-tree` lays out comparably and is not the O(N^3) worst case network-simplex can be).
 * Pure: dagre reads no DOM, so this runs in a test as it does in the browser.
 */
export function layoutGraph(graph: AgentGraph): GraphLayout {
  const placed = new Graph();
  placed.setGraph({rankdir: 'LR', ranksep: RANK_GAP, nodesep: NODE_GAP, ranker: 'tight-tree'});
  placed.setDefaultEdgeLabel(() => ({}));
  for (const node of graph.nodes) placed.setNode(node.id, {...NODE});
  for (const edge of graph.edges) placed.setEdge(edge.from, edge.to);
  layout(placed);
  const size = placed.graph();
  return {
    width: size.width ?? 0,
    height: size.height ?? 0,
    // dagre centres a node on its box; the DOM positions it by its corner.
    nodes: graph.nodes.map(node => {
      const spot = placed.node(node.id);
      return {...node, x: spot.x - NODE.width / 2, y: spot.y - NODE.height / 2};
    }),
    edges: graph.edges.map(edge => ({
      ...edge,
      points: placed.edge(edge.from, edge.to).points ?? [],
    })),
  };
}

type Facts = Map<number, {entry: HypothesisEntry; round: HypothesisRound}>;

function roundFacts(experiments: readonly HypothesisEntry[]): Facts {
  const facts: Facts = new Map();
  for (const entry of experiments) {
    for (const round of entry.rounds ?? []) facts.set(round.round, {entry, round});
  }
  return facts;
}

function roundStatus(
  round: RoundState | undefined,
  fact: HypothesisRound | undefined,
  status: CoreRunStatus,
  ended: boolean,
): RoundStatus {
  const finished = round === undefined || round.status === 'completed' || round.status === 'failed';
  if (!finished) {
    // A round the run ended inside never finishes.
    if (ended) return 'failed';
    return status === 'paused' ? 'paused' : 'running';
  }
  if (round?.status === 'failed' || fact?.passed === false) return 'failed';
  if (fact?.judge_verdict === 'fail' || fact?.candidate_disposition === 'discard')
    return 'rejected';
  // ponytail: a finished round whose experiments row has not been refetched yet reads as kept
  // until experiments_changed(round_persisted) lands, which the backend emits with the round.
  return 'kept';
}

/** The latest kept round (or R0) with a value before `round`: what `round` is compared against. */
function incumbentBefore(rows: readonly RailRow[], round: number): number | null {
  return (
    rows
      .filter(
        row =>
          row.round < round &&
          (row.status === 'kept' || row.status === 'baseline') &&
          row.value !== null,
      )
      .at(-1)?.round ?? null
  );
}

export function railModel(
  core: CoreState,
  experiments: readonly HypothesisEntry[],
  context: PerformanceContext | null,
): RailModel {
  const facts = roundFacts(experiments);
  const ended = hasRunEnded(core);
  const byNumber = new Map(core.rounds.map(round => [round.number, round]));
  const numbers = new Set(facts.keys());
  for (const round of core.rounds) if (round.status !== 'planned') numbers.add(round.number);
  const rows = [...numbers]
    .sort((left, right) => left - right)
    .map((number): RailRow => {
      const round = byNumber.get(number);
      const fact = facts.get(number)?.round;
      const status = roundStatus(round, fact, core.status, ended);
      const value = typeof fact?.perf_metric === 'number' ? fact.perf_metric : null;
      const unit = fact?.perf_unit ? ` ${fact.perf_unit}` : '';
      return {
        round: number,
        status,
        value: value === null ? null : formatValue(value),
        valueTip: value === null ? null : `${exact.format(value)}${unit}`,
        official: fact?.official_evaluation === true,
        incumbent: false,
        live: (status === 'running' || status === 'paused') && round !== undefined ? round : null,
      };
    });
  const baseline = context?.objective_baseline_value;
  if (typeof baseline === 'number') {
    const unit = context?.objective_unit ? ` ${context.objective_unit}` : '';
    rows.unshift({
      round: 0,
      status: 'baseline',
      value: formatValue(baseline),
      valueTip: `${exact.format(baseline)}${unit}`,
      official: false,
      incumbent: false,
      live: null,
    });
  }
  // R0 is the incumbent until the first kept round with a value.
  const incumbent = incumbentBefore(rows, Number.POSITIVE_INFINITY);
  for (const row of rows) row.incumbent = row.round === incumbent;
  const latest = rows.at(-1)?.round ?? null;
  const roundsLeft =
    ended || core.maxRounds === null || latest === null
      ? null
      : Math.max(0, core.maxRounds - latest);
  return {rows, roundsLeft};
}

// The sparkline's user space: a `0 0 100 36` viewBox, inset so the stroke and the dot at the last
// point are never clipped. y is inverted: the largest value sits at `TOP`.
const LEFT = 3;
const RIGHT = 97;
const TOP = 4;
const BOTTOM = 32;

/** `value` on `[min, max]` placed on `[low, high]`; a range of one value sits at the middle. */
function scale(value: number, min: number, max: number, low: number, high: number): number {
  const at = max === min ? (low + high) / 2 : low + ((value - min) / (max - min)) * (high - low);
  return Math.round(at * 100) / 100;
}

/**
 * The metric across rounds, for the rail's sparkline: one point per round the series measured, in
 * round order, R0 first when the objective records a baseline. Rounds without a value are skipped,
 * never interpolated, so x follows the round number and a gap reads as a longer segment.
 */
export function trendModel(
  performance: readonly PerformanceRound[],
  context: PerformanceContext | null,
): TrendModel | null {
  const series = performance
    .filter(row => Number.isFinite(row.perf_metric))
    .map(row => ({round: row.round, value: row.perf_metric}))
    .sort((left, right) => left.round - right.round);
  const baseline = context?.objective_baseline_value;
  if (typeof baseline === 'number') series.unshift({round: 0, value: baseline});
  // Fewer than two points is no curve at all.
  const [head, ...rest] = series;
  const tail = rest.at(-1);
  if (head === undefined || tail === undefined) return null;
  const values = series.map(point => point.value);
  const rounds = series.map(point => point.round);
  const [low, high] = [Math.min(...values), Math.max(...values)];
  const [start, end] = [Math.min(...rounds), Math.max(...rounds)];
  return {
    points: series.map(point => ({
      x: scale(point.round, start, end, LEFT, RIGHT),
      y: scale(point.value, low, high, BOTTOM, TOP),
    })),
    first: formatValue(head.value),
    last: formatValue(tail.value),
    firstRound: head.round,
    lastRound: tail.round,
  };
}

/** What the rail shows from the experiments query: `experiments_ready: false` is unattached. */
export function railState(experiments: ProtocolResponse | null, error: string | null): RailState {
  if (experiments === null) return error === null ? 'loading' : 'error';
  return experiments.experiments_ready === false ? 'unattached' : 'ready';
}

export function steers(captured: readonly RunEvent[]): Steers {
  let pending: PendingSteer[] = [];
  const consumed: ConsumedSteer[] = [];
  for (const event of captured) {
    if (event.type !== 'control' || !event.text?.startsWith('/steer')) continue;
    if (event.status === 'pending' && event.text.startsWith('/steer: ')) {
      pending.push({id: `steer-${event.sequence}`, text: event.text.slice('/steer: '.length)});
    } else if (event.status === 'consumed') {
      // One consumed event delivers every steer queued since the previous one.
      for (const steer of pending) {
        consumed.push({
          ...steer,
          sequence: event.sequence ?? 0,
          round: roundNumberFromLabel(event.round_label),
          roundLabel: event.round_label ?? null,
          agentKind: event.agent_kind ?? null,
        });
      }
      pending = [];
    }
  }
  return {pending, consumed};
}

const INLINE = /(`[^`\n]+`|\*\*[^*\n]+\*\*)/;

/** Paragraphs of inline code, bold, and plain text. Nothing else of Markdown is rendered. */
export function prose(text: string): ProsePart[][] {
  return text
    .trim()
    .split(/\n{2,}/)
    .map(paragraph =>
      paragraph
        .split(INLINE)
        .filter(token => token !== '')
        .map((token): ProsePart => {
          if (token.length > 2 && token.startsWith('`') && token.endsWith('`')) {
            return {kind: 'code', text: token.slice(1, -1)};
          }
          if (token.length > 4 && token.startsWith('**') && token.endsWith('**')) {
            return {kind: 'strong', text: token.slice(2, -2)};
          }
          return {kind: 'text', text: token};
        }),
    );
}

/** Strips the run workspace prefix (`/…/<run id>/`) from paths at the start of a word. */
export function pathShortener(runId: string | null): (text: string) => string {
  if (runId === null) return text => text;
  const escaped = runId.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const prefix = new RegExp(String.raw`(?<=^|[\s'"=(])/[^\s'"]*?/${escaped}/`, 'g');
  return text => text.replace(prefix, '');
}

const results = new WeakMap<TranscriptEntry, ToolResultSummary | null>();

/** "12 passed", "3 failed", "Error", or "Exit 2"; null when the result says nothing short. */
export function toolResult(entry: TranscriptEntry): ToolResultSummary | null {
  const cached = results.get(entry);
  if (cached !== undefined) return cached;
  const summary = summarize(entry);
  results.set(entry, summary);
  return summary;
}

function summarize(entry: TranscriptEntry): ToolResultSummary | null {
  const result = entry.toolResult;
  if (result === undefined) return null;
  const payload = result.payload;
  const command = payload?.kind === 'command' ? payload : null;
  const output = command === null ? result.content : `${command.stdout}\n${command.stderr}`;
  // ponytail: the first "N passed" wins; a command running several suites reports its first one.
  const failed = /(\d+) failed/.exec(output);
  if (failed && Number(failed[1]) > 0) return {text: `${failed[1]} failed`, failed: true};
  const passed = /(\d+) passed/.exec(output);
  if (passed) return {text: `${passed[1]} passed`, failed: false};
  if (result.is_error) return {text: 'Error', failed: true};
  if (command?.exit_code != null && command.exit_code !== 0) {
    return {text: `Exit ${command.exit_code}`, failed: true};
  }
  return null;
}

/** A flag, an operator, a redirect, an assignment, a quoted word: not a path the command named. */
const NOT_A_PATH = /^[-<>|&;]|^\d*[<>]|[='"$()]/;
/** `src/lib.rs`, `~/.cache/uv`, `Cargo.toml`: a path, or a bare name with a short extension. */
const A_PATH = /\/|\.[A-Za-z0-9]{1,5}$/;

/**
 * A command's shape rather than its text: the executable and the first path it names, cut to
 * that path's last two segments. Everything a row drops here is in its tooltip.
 */
function bashTarget(command: string): string | null {
  const tokens = command
    .trim()
    .split(/\s+/)
    .map(token => token.replace(/^['"]+|['";,]+$/g, ''));
  // `TMPDIR=/tmp make …`: the assignments in front of a command are not the command.
  while (/^\w+=/.test(tokens[0] ?? '')) tokens.shift();
  const executable = tokens.shift();
  if (!executable) return null;
  const path = tokens.find(token => token !== '' && !NOT_A_PATH.test(token) && A_PATH.test(token));
  if (path === undefined) return executable;
  const parts = path.replace(/\/+$/, '').split('/');
  return `${executable} ${parts.length > 2 ? `…/${parts.slice(-2).join('/')}` : parts.join('/')}`;
}

/** The row's verb, what it shows for a target, and the whole target when it shows less. */
function toolLabel(entry: TranscriptEntry): [string, string | null, string | null] {
  const args = entry.toolArguments ?? {};
  const text = (key: string): string | null => {
    const value = args[key];
    return typeof value === 'string' ? value : null;
  };
  const plainly = (verb: string, arg: string | null): [string, string | null, null] => [
    verb,
    arg,
    null,
  ];
  switch (entry.toolName) {
    case 'Bash': {
      const command = text('command');
      const target = command === null ? null : bashTarget(command);
      return [text('description') ?? 'Ran a command', target, target === command ? null : command];
    }
    case 'Write':
      return plainly('Wrote', text('file_path'));
    case 'Edit':
    case 'MultiEdit':
      return plainly('Edited', text('file_path'));
    case 'Read':
      return plainly('Read', text('file_path'));
    case 'Grep':
    case 'Glob':
      return plainly('Searched', text('pattern'));
    case 'StructuredOutput':
      return plainly(
        entry.agentKind === 'implementer'
          ? 'Returned the attempt report'
          : 'Returned structured output',
        null,
      );
    case undefined:
      return plainly('Tool call', entry.toolCall?.replace(/^\s*→\s*/, '').trim() || null);
    default: {
      const first = Object.values(args).find(value => typeof value === 'string');
      return plainly(entry.toolName, typeof first === 'string' ? first : null);
    }
  }
}

/** `round-N-retry-K[-role]` is attempt K; `round-N-retry-K-plan` is a plan reprompt, attempt 1. */
function attemptOf(roundLabel: string | null | undefined): number {
  const match = roundLabel?.match(/-retry-(\d+)(?!\d|-plan)/);
  return match ? Number(match[1]) : 1;
}

function plain(parts: readonly ProsePart[]): string {
  return parts.map(part => part.text).join('');
}

interface Row {
  sequence: number;
  role: string;
  attempt: number;
  item: LogItem | null;
}

/**
 * The selected round's log: role groups in sequence order, consumed steers at the call that
 * consumed them, one "Attempt N" divider per retry, and every group but the last collapsed.
 */
export function logGroups(
  core: CoreState,
  consumed: readonly ConsumedSteer[],
  round: number,
  runId: string | null,
): LogGroup[] {
  const short = pathShortener(runId);
  const active = Object.values(core.activeExecutions);
  const running = new Set(active.map(execution => execution.executionId));
  const entries = core.transcript.filter(
    entry => entry.roundNumber === round && entry.agentKind !== undefined,
  );
  // The in-flight call: the last open tool call whose execution is still running.
  let inFlight: string | null = null;
  for (const entry of entries) {
    const open = entry.kind === 'tool' && !entry.toolResult && entry.toolResponse === undefined;
    if (open && entry.invocationId !== undefined && running.has(entry.invocationId)) {
      inFlight = entry.id;
    }
  }
  const rows: Row[] = [];
  for (const entry of entries) {
    const sequence = Number(entry.id);
    const base = {
      sequence: Number.isFinite(sequence) ? sequence : Number.POSITIVE_INFINITY,
      role: entry.agentKind ?? 'agent',
      attempt: attemptOf(entry.roundLabel),
    };
    if (entry.kind === 'tool') {
      const [verb, arg, argFull] = toolLabel(entry);
      rows.push({
        ...base,
        item: {
          kind: 'tool',
          id: entry.id,
          verb,
          arg: arg === null ? null : short(arg),
          argFull: argFull === null ? null : short(argFull),
          result: toolResult(entry),
          inFlight: entry.id === inFlight,
        },
      });
    } else if ((entry.kind === 'assistant' || entry.kind === 'analysis') && entry.content.trim()) {
      rows.push({...base, item: {kind: 'prose', id: entry.id, paragraphs: prose(entry.content)}});
    } else if (entry.kind === 'status') {
      // A phase start ends the previous role's group; its own group shows once it has entries.
      rows.push({...base, item: null});
    }
  }
  for (const steer of consumed) {
    if (steer.round !== round) continue;
    rows.push({
      sequence: steer.sequence,
      role: steer.agentKind ?? 'agent',
      attempt: attemptOf(steer.roundLabel),
      item: {kind: 'steer', id: steer.id, text: steer.text},
    });
  }
  rows.sort((left, right) => left.sequence - right.sequence);

  const drafts: LogGroup[] = [];
  for (const row of rows) {
    let group = drafts.at(-1);
    if (group === undefined || group.role !== row.role || group.attempt !== row.attempt) {
      group = {
        id: `${row.role}-${row.sequence}`,
        role: row.role,
        attempt: row.attempt,
        divider: null,
        collapsed: false,
        active: false,
        summary: '',
        calls: 0,
        items: [],
      };
      drafts.push(group);
    }
    if (row.item === null) continue;
    group.items.push(row.item);
    if (row.item.kind === 'tool') group.calls += 1;
  }
  const live = !hasRunEnded(core) && latestRound(core) === round;
  const acting = (role: string) =>
    live &&
    active.some(execution => execution.agentKind === role && execution.roundNumber === round);
  // A role without entries shows only while it acts, so the log names who is working before
  // its first entry.
  const groups = drafts.filter(
    (group, index) => group.items.length > 0 || (index === drafts.length - 1 && acting(group.role)),
  );
  groups.forEach((group, index) => {
    const previous = groups[index - 1];
    const last = index === groups.length - 1;
    group.divider =
      group.attempt > 1 && (previous === undefined || previous.attempt !== group.attempt)
        ? group.attempt
        : null;
    group.collapsed = !last;
    group.active = last && acting(group.role);
    const prose = group.items.filter(item => item.kind === 'prose').at(-1);
    const tool = group.items.filter(item => item.kind === 'tool').at(-1);
    group.summary =
      prose?.kind === 'prose'
        ? (plain(prose.paragraphs[0] ?? []).split('\n')[0] ?? '')
        : tool?.kind === 'tool'
          ? tool.verb
          : '';
  });
  return groups;
}

/**
 * Whether the round's history may sit below the tail floor and needs `loadOlder()`: true until
 * an earlier round's entries above the floor show where this round starts. Spine entries at or
 * below the floor (a replayed `round_finished`) say nothing about what was skipped.
 */
export function needsOlder(core: CoreState, round: number): boolean {
  const floor = core.historyAfterSequence;
  // R0 is the objective's baseline, not a round: it has no events.
  if (floor === 0 || round === 0) return false;
  const earliest = core.transcript.find(
    entry => entry.roundNumber !== undefined && Number(entry.id) > floor,
  )?.roundNumber;
  return earliest === undefined || round <= earliest;
}

/**
 * Whether the text of a steer `round` consumed may still sit below the tail floor. The backend
 * drains queued steers when an agent call starts and journals only `/steer` on the consuming
 * call, so what call C consumed was queued after the call before C started: backfill until that
 * start (its `phase_started` entry) is above the floor. `needsOlder` already covers a C-1 inside
 * `round`, and a still-queued steer (sent after the latest call started), so this adds R-1's last
 * call when `round`'s first call consumed a steer.
 */
export function steersNeedOlder(
  core: CoreState,
  captured: readonly RunEvent[],
  round: number,
): boolean {
  const floor = core.historyAfterSequence;
  if (floor === 0) return false;
  const consumed = captured.find(
    event =>
      event.type === 'control' &&
      event.status === 'consumed' &&
      event.text?.startsWith('/steer') === true &&
      roundNumberFromLabel(event.round_label) === round,
  );
  const at = consumed?.sequence;
  if (consumed === undefined || at === undefined) return false;
  // ponytail: a steer sent between C-1's drain and its phase_started event sits just below that
  // start; core-state keeps no earlier call boundary, so such a steer can stay unloaded.
  return !core.transcript.some(
    entry =>
      entry.kind === 'status' &&
      entry.roundNumber !== undefined &&
      Number(entry.id) > floor &&
      Number(entry.id) < at &&
      // The consuming call's own start can precede its control event.
      !(entry.roundLabel === consumed.round_label && entry.agentKind === consumed.agent_kind),
  );
}

function judgeAttempts(
  captured: readonly RunEvent[],
  round: number,
  status: RoundStatus | undefined,
): JudgeAttempt[] {
  const verdicts = captured.flatMap(event =>
    event.data?.kind === 'judge_result' && roundNumberFromLabel(event.round_label) === round
      ? [event.data]
      : [],
  );
  return verdicts.map((result, index) => {
    const last = index === verdicts.length - 1;
    const verdict: Verdict =
      result.verdict === 'fail'
        ? 'Gate failed'
        : !last
          ? 'Passed'
          : status === 'kept'
            ? 'Kept'
            : status === 'rejected'
              ? 'Rejected'
              : status === 'failed'
                ? 'Gate failed'
                : 'Passed';
    return {attempt: result.attempt, verdict, feedback: result.feedback, open: last};
  });
}

export function inspectorModel(
  rows: readonly RailRow[],
  experiments: readonly HypothesisEntry[],
  design: readonly DesignRound[],
  captured: readonly RunEvent[],
  round: number,
  context: PerformanceContext | null,
): InspectorModel {
  const facts = roundFacts(experiments);
  const row = rows.find(candidate => candidate.round === round);
  const fact = facts.get(round);
  const live = row?.status === 'running' || row?.status === 'paused';
  const entry =
    fact?.entry ??
    experiments.find(
      candidate => candidate.first_round <= round && round <= candidate.last_round,
    ) ??
    (live ? experiments.find(candidate => candidate.active === true) : undefined);
  const name =
    entry?.perf_metric_name ?? context?.objective_metric ?? fact?.round.perf_unit ?? null;
  const direction = entry?.perf_direction ?? context?.objective_direction ?? null;
  // The incumbent of that time: the latest kept round (or R0) before this one with a value.
  const incumbent = incumbentBefore(rows, round);
  const changes = design.find(candidate => candidate.round === round);
  const delta = live
    ? {value: null, vs: incumbent, tip: null}
    : measuredDelta(facts, round, incumbent, context);
  return {
    round,
    hypothesis: entry === undefined ? null : hypothesisText(entry),
    // The metric names the delta; with no delta it would head nothing.
    metric: name === null || delta === null ? null : {name, direction},
    delta,
    // The final attempt restates the round's outcome only from its experiments row; without one
    // (unattached, or before the refetch) the judge's own verdict stands.
    judge: judgeAttempts(captured, round, fact === undefined ? undefined : row?.status),
    changes:
      showsChanges(row) && changes?.files != null
        ? {commit: changes.commit ?? null, files: changes.files}
        : null,
  };
}

/** Changes resolve when a round ends, and R0 has none: only finished rounds past R0 show them. */
export function showsChanges(row: RailRow | undefined): boolean {
  return row !== undefined && row.round > 0 && row.status !== 'running' && row.status !== 'paused';
}

/** Title and claim, with a title the server derived from the claim folded into the claim. */
function hypothesisText(entry: HypothesisEntry): NonNullable<InspectorModel['hypothesis']> {
  const id = entry.identified === false ? null : entry.hypothesis_id;
  const title = entry.title ?? null;
  const claim = entry.claim ?? null;
  const derived =
    title !== null &&
    claim !== null &&
    (title === claim || (title.endsWith('…') && claim.startsWith(title.slice(0, -1))));
  return derived ? {id, title: claim, claim: null} : {id, title, claim};
}

function measuredDelta(
  facts: Facts,
  round: number,
  incumbent: number | null,
  context: PerformanceContext | null,
): InspectorModel['delta'] {
  const measured = facts.get(round)?.round;
  const value = measured?.perf_metric;
  const base =
    incumbent === 0
      ? context?.objective_baseline_value
      : incumbent === null
        ? undefined
        : facts.get(incumbent)?.round.perf_metric;
  if (typeof value !== 'number' || typeof base !== 'number' || base === 0) return null;
  const unit = measured?.perf_unit ? ` ${measured.perf_unit}` : '';
  return {
    value: formatDelta((value / base - 1) * 100),
    vs: incumbent,
    tip: `${formatValue(value)} vs ${formatValue(base)}${unit}`,
  };
}

export function endedWord(core: CoreState, captured: readonly RunEvent[]): EndedWord | null {
  if (!hasRunEnded(core)) return null;
  if (captured.some(event => event.type === 'run_interrupted')) return 'Interrupted';
  return core.status === 'completed' ? 'Completed' : 'Failed';
}

/** The one run control, from `core.status` and the connection only. Resume only in `paused`. */
export function runControl(
  core: CoreState,
  captured: readonly RunEvent[],
  connection: Connection,
): RunControl {
  const word = endedWord(core, captured);
  if (word !== null) {
    const diagnostic = core.diagnostics.filter(candidate => candidate.scope === 'run').at(-1);
    const tip = diagnostic ? (diagnostic.detail ?? diagnostic.summary) : null;
    return {kind: 'ended', word, tip: word === 'Completed' ? null : tip};
  }
  const offline = connection !== 'connected';
  switch (core.status) {
    case 'running':
      return {
        kind: 'action',
        action: 'pause',
        label: 'Pause',
        tip: 'Pause after the current agent call',
        disabled: offline,
      };
    case 'pausing':
      return {
        kind: 'action',
        action: 'pause',
        label: 'Pausing',
        tip: 'Pausing after the current agent call',
        disabled: true,
      };
    case 'paused':
      return {
        kind: 'action',
        action: 'resume',
        label: 'Resume',
        tip: 'Resume the run',
        disabled: offline,
      };
    default:
      return {
        kind: 'action',
        action: 'pause',
        label: 'Pause',
        tip: 'Pause after the current agent call',
        disabled: true,
      };
  }
}

const TERMINAL = new Set(['run_finished', 'run_failed', 'run_interrupted', 'configuration_failed']);

/**
 * The objective as tooltip text, and its first sentence (up to the first `.`, `!`, or `?` before a
 * space or line end). Soft wraps become spaces; paragraph breaks and list-item lines (`- `, `* `,
 * `1. `) stay; inline backticks are dropped.
 */
function objective(text: string | null | undefined): HeaderModel['objective'] {
  const full = text
    ?.split(/\n[ \t]*\n/)
    .map(paragraph => paragraph.trim().replace(/[ \t]*\n(?![ \t]*(?:[-*] |\d+\. ))[ \t]*/g, ' '))
    .filter(Boolean)
    .join('\n\n')
    .replace(/`([^`]*)`/g, '$1');
  if (!full) return null;
  const line = full.split('\n')[0] ?? full;
  // ponytail: an abbreviation such as "e.g." ends the sentence early; the tooltip has the rest.
  const first = /^.*?[.!?](?=\s|$)/.exec(line)?.[0] ?? line;
  return {first, full};
}

export function headerModel(
  core: CoreState,
  captured: readonly RunEvent[],
  connection: Connection,
  context: PerformanceContext | null,
): HeaderModel {
  const started = captured.find(event => event.type === 'run_started');
  const input = started?.data?.kind === 'run_started' ? started.data.input : null;
  return {
    project: input?.split('/').filter(Boolean).at(-1) ?? null,
    objective: objective(context?.objective_description),
    startedAt: started?.timestamp ?? null,
    endedAt: hasRunEnded(core)
      ? (captured.find(event => TERMINAL.has(event.type))?.timestamp ?? null)
      : null,
    control: runControl(core, captured, connection),
  };
}

const STATUS_WORDS: Partial<Record<CoreRunStatus, string>> = {
  running: 'Running',
  pausing: 'Pausing after the current agent call',
  paused: 'Paused',
};

/**
 * What the polite live region says for one change: run status, new rounds, and a dropped
 * connection (once per drop; the banner's exhausted and protocol-error states are alerts).
 * `''` clears the region silently; null leaves it as it is.
 */
export function announce(previous: RunPulse | null, next: RunPulse): string | null {
  if (previous === null) return null;
  if (previous.connection === 'connected' && next.connection === 'disconnected') {
    return 'Reconnecting…';
  }
  // Back online, "Reconnecting…" must not linger, and the next drop must change the text again.
  const cleared =
    previous.connection !== 'connected' && next.connection === 'connected' ? '' : null;
  if (previous.status === 'connecting') return cleared;
  const parts: string[] = [];
  if (next.ended === null && next.round !== null && next.round !== previous.round) {
    parts.push(`Round ${next.round} started`);
  }
  if (next.ended !== null && previous.ended === null) parts.push(`Run ${next.ended.toLowerCase()}`);
  else if (next.ended === null && next.status !== previous.status) {
    const word = STATUS_WORDS[next.status];
    if (word !== undefined) parts.push(word);
  }
  return parts.length === 0 ? cleared : parts.join('. ');
}
