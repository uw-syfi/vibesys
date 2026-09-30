/** A round's agent executions as a top-to-bottom graph: pure, dagre reads no DOM. */
import {Graph, layout} from '@dagrejs/dagre';
import {
  type AgentPhase,
  type AgentPhaseStatus,
  type CoreState,
  phasesForRound,
} from '@vibesys/core-state';
import {titleCase} from './derive.js';
import {phaseKeys, phaseName} from './transcript.js';

export const AGENT_NODE = {width: 140, height: 44} as const;

export interface AgentNode {
  /** The execution's key, shared with its transcript turn. */
  id: string;
  role: string;
  /** Short phase for the card: Reviewing, Planning, Attempt N. */
  phase: string;
  /** Long phase for the detail heading. */
  phaseLong: string;
  status: AgentPhaseStatus;
  label: string | null;
  runtime: string | null;
  wall: string;
  x: number;
  y: number;
}

interface AgentEdge {
  id: string;
  source: string;
  target: string;
}

export interface AgentGraph {
  nodes: AgentNode[];
  edges: AgentEdge[];
  width: number;
  height: number;
}

const HARNESSES: Readonly<Record<string, string>> = {
  codex: 'Codex',
  claude: 'Claude Code',
  gemini: 'Gemini',
  opencode: 'Opencode',
};

interface Span {
  start: number;
  /** Infinity while running; the start when a finished phase recorded no end. */
  end: number;
}

function spanOf(phase: AgentPhase): Span {
  const start = Date.parse(phase.startedAt ?? '');
  if (phase.status === 'active') return {start, end: Number.POSITIVE_INFINITY};
  const end = Date.parse(phase.finishedAt ?? '');
  return {start, end: Number.isNaN(end) ? start : end};
}

/** `from` ended before `to` started; unknown times never order anything. */
const before = (from: Span, to: Span) => from.end <= to.start;

/**
 * Inferred handovers. Events carry no dependency graph, so an edge only joins two executions
 * that did not overlap in time: `to` has an edge from each `from` that ended before it started
 * with no third execution fitting between them. Overlapping executions (fan-out, or one long
 * execution beside short ones) get no edge between them.
 */
export function inferredEdges<T>(items: ReadonlyArray<{key: T; span: Span}>): Array<[T, T]> {
  const edges: Array<[T, T]> = [];
  for (const to of items) {
    const candidates = items.filter(from => from !== to && before(from.span, to.span));
    for (const from of candidates) {
      const covered = candidates.some(
        middle => middle !== from && before(from.span, middle.span) && before(middle.span, to.span),
      );
      if (!covered) edges.push([from.key, to.key]);
    }
  }
  return edges;
}

function runtime(phase: AgentPhase): string | null {
  const provider = phase.provider?.trim();
  const harness = provider ? (HARNESSES[provider.toLowerCase()] ?? titleCase(provider)) : null;
  const via = [harness, phase.driver ? phase.driver.toUpperCase() : null].filter(Boolean).join(' ');
  return [phase.model, via].filter(Boolean).join(', ') || null;
}

function wallTime(phase: AgentPhase): string {
  if (phase.status === 'active') return 'running';
  const start = Date.parse(phase.startedAt ?? '');
  const end = Date.parse(phase.finishedAt ?? '');
  if (Number.isNaN(start) || Number.isNaN(end)) return 'unknown';
  return `${((end - start) / 1000).toFixed(1)}s`;
}

interface Layout {
  key: string;
  /** Top-left corner of each card. */
  at: Map<string, {x: number; y: number}>;
  width: number;
  height: number;
}

/** The last layout: a live round's event batches rarely change the graph, so dagre rarely reruns. */
let lastLayout: Layout | null = null;

/** Positions depend only on the node ids and edges (every card has the same size). */
function layoutOf(ids: string[], edges: readonly AgentEdge[]): Layout {
  const key = JSON.stringify([ids, edges.map(edge => [edge.source, edge.target])]);
  if (lastLayout?.key === key) return lastLayout;
  const graph = new Graph();
  graph.setGraph({rankdir: 'TB', nodesep: 16, ranksep: 24, marginx: 16, marginy: 16});
  graph.setDefaultEdgeLabel(() => ({}));
  for (const id of ids) graph.setNode(id, {...AGENT_NODE});
  for (const edge of edges) graph.setEdge(edge.source, edge.target);
  layout(graph);
  const at = new Map(
    ids.map(id => {
      const spot = graph.node(id);
      return [id, {x: spot.x - AGENT_NODE.width / 2, y: spot.y - AGENT_NODE.height / 2}];
    }),
  );
  const size = graph.graph();
  lastLayout = {key, at, width: size.width ?? 0, height: size.height ?? 0};
  return lastLayout;
}

export function agentGraph(core: CoreState, round: number | null): AgentGraph {
  const keys = phaseKeys(phasesForRound(core.phases, round));
  if (keys.size === 0) return {nodes: [], edges: [], width: 0, height: 0};
  const items = [...keys].map(([phase, key]) => ({key, span: spanOf(phase)}));
  const edges = inferredEdges(items).map(([source, target]) => ({
    id: `${source}>${target}`,
    source,
    target,
  }));
  const spots = layoutOf([...keys.values()], edges);
  const nodes = [...keys].map(([phase, id]): AgentNode => {
    const spot = spots.at.get(id) ?? {x: 0, y: 0};
    return {
      id,
      role: titleCase(phase.kind),
      phase: phase.roundLabel?.endsWith('-pre') ? 'Reviewing' : phaseName(phase.roundLabel, round),
      phaseLong: phaseName(phase.roundLabel, round),
      status: phase.status,
      label: phase.roundLabel,
      runtime: runtime(phase),
      wall: wallTime(phase),
      x: spot.x,
      y: spot.y,
    };
  });
  return {nodes, edges, width: spots.width, height: spots.height};
}
