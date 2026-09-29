import '@xyflow/react/dist/base.css';
import type {CoreState} from '@vibesys/core-state';
import {
  type Edge,
  Handle,
  MarkerType,
  type Node,
  type NodeProps,
  Position,
  ReactFlow,
} from '@xyflow/react';
import {Check} from 'lucide-react';
import {type ReactNode, useMemo} from 'react';
import {AGENT_NODE, type AgentGraph, type AgentNode, agentGraph} from '../agents.js';
import type {Turn} from '../transcript.js';
import {PaneHead} from './Pane.js';
import {DisclosureBodies, Disclosures, type TranscriptControls} from './Transcript.js';

type CardData = {node: AgentNode; selected: boolean; onSelect: (id: string) => void};
type CardNode = Node<CardData, 'agent'>;

const EDGE_STYLE = {stroke: 'var(--line)', strokeWidth: 1.5, strokeDasharray: '3 3'};
const MARKER = {type: MarkerType.ArrowClosed, width: 14, height: 14, color: 'var(--line)'};
const BROKEN = new Set(['failed', 'cancelled', 'interrupted']);

function NodeGlyph({status}: {status: AgentNode['status']}) {
  if (status === 'active') return <span className="dot live" role="img" aria-label="active" />;
  if (BROKEN.has(status)) return <span className="dot err" role="img" aria-label={status} />;
  return <Check size={12} strokeWidth={2.5} className="t2" role="img" aria-label={status} />;
}

/** A card is a button, so the graph is reachable by keyboard; clicking it filters the transcript. */
function AgentCard({data}: NodeProps<CardNode>) {
  const {node, selected, onSelect} = data;
  const className = `ag${node.status === 'active' ? ' act' : ''}${selected ? ' sel' : ''}`;
  return (
    <>
      <Handle type="target" position={Position.Top} />
      <button
        type="button"
        className={className}
        aria-pressed={selected}
        title={`${node.label ?? node.role}\nShow only this agent's turns`}
        onClick={() => onSelect(node.id)}
      >
        <span className="r">
          {node.role}
          <NodeGlyph status={node.status} />
        </span>
        <span className="m">{node.phase}</span>
      </button>
      <Handle type="source" position={Position.Bottom} />
    </>
  );
}

const NODE_TYPES = {agent: AgentCard};

export interface AgentsProps {
  round: number;
  graph: AgentGraph;
  selected: string | null;
  /** The pane's width, to centre the graph at zoom 1. */
  width: number;
  onSelect: (id: string) => void;
  detail: ReactNode;
}

export function Agents({round, graph, selected, width, onSelect, detail}: AgentsProps) {
  const nodes = useMemo<CardNode[]>(
    () =>
      graph.nodes.map(node => ({
        id: node.id,
        type: 'agent',
        position: {x: node.x, y: node.y},
        data: {node, selected: node.id === selected, onSelect},
        width: AGENT_NODE.width,
        height: AGENT_NODE.height,
        draggable: false,
        selectable: false,
        // React Flow turns pointer events off on nodes that are neither draggable nor selectable;
        // the card is a button, so it takes them back.
        style: {pointerEvents: 'all'},
        // Per element as well as on ReactFlow, whose flags reach its store only after mount.
        focusable: false,
      })),
    [graph, selected, onSelect],
  );
  const edges = useMemo<Edge[]>(
    () =>
      graph.edges.map(edge => ({...edge, style: EDGE_STYLE, markerEnd: MARKER, focusable: false})),
    [graph],
  );
  const count = `${graph.nodes.length} invocation${graph.nodes.length === 1 ? '' : 's'}`;
  return (
    <>
      <PaneHead scope={`Round ${round}`}>
        <span title="Events carry no dependency graph: arrows follow start times">
          {count}, order inferred
        </span>
      </PaneHead>
      {graph.nodes.length === 0 ? (
        <p className="empty1">No agent has started in round {round} yet.</p>
      ) : (
        // Zoom and pan are off, so a graph wider than the pane (a wide fan-out) scrolls instead.
        <figure className="graphwrap" aria-label={`Round ${round} agent invocations`}>
          <div
            className="graphcanvas"
            style={{width: Math.max(width, graph.width), height: graph.height}}
          >
            <ReactFlow
              key={`${round}:${width}`}
              nodes={nodes}
              edges={edges}
              nodeTypes={NODE_TYPES}
              defaultViewport={{x: Math.max(0, (width - graph.width) / 2), y: 0, zoom: 1}}
              minZoom={1}
              maxZoom={1}
              nodesDraggable={false}
              nodesConnectable={false}
              elementsSelectable={false}
              // Read-only, and each card is its own button: React Flow's focusable wrappers, edges and
              // "press delete" descriptions would add tab stops and instructions for nothing.
              nodesFocusable={false}
              edgesFocusable={false}
              deleteKeyCode={null}
              disableKeyboardA11y
              zoomOnScroll={false}
              zoomOnDoubleClick={false}
              zoomOnPinch={false}
              panOnDrag={false}
              panOnScroll={false}
              preventScrolling={false}
              proOptions={{hideAttribution: true}}
            />
          </div>
        </figure>
      )}
      {detail}
    </>
  );
}

function toolCalls(turn: Turn): number {
  return turn.items.reduce(
    (count, item) => count + (item.kind === 'tools' ? item.tools.length : 0),
    0,
  );
}

export interface AgentDetailProps {
  node: AgentNode | null;
  turn: Turn | null;
  filtered: boolean;
  controls: TranscriptControls;
  onFilter: () => void;
}

export function AgentDetail({node, turn, filtered, controls, onFilter}: AgentDetailProps) {
  if (node === null)
    return <p className="empty1">Select an invocation to see its prompt and tool calls.</p>;
  return (
    <div className="detail">
      <h3>
        {node.role}
        <span>{node.phaseLong}</span>
      </h3>
      <dl className="kv">
        <dt>Invocation</dt>
        <dd className="mono" title={node.id}>
          {node.id.length > 12 ? `${node.id.slice(0, 12)}…` : node.id}
        </dd>
        <dt>Label</dt>
        <dd className="mono">{node.label ?? 'none'}</dd>
        <dt>Model</dt>
        <dd>{node.runtime ?? 'not recorded'}</dd>
        <dt>Tool calls</dt>
        <dd className="num">{turn === null ? 0 : toolCalls(turn)}</dd>
        <dt>Wall time</dt>
        <dd className="num">{node.wall}</dd>
      </dl>
      <div className="detailacts">
        {turn === null ? null : <Disclosures turn={turn} controls={controls} />}
        <button type="button" className="disc" onClick={onFilter}>
          {filtered ? 'Show all turns' : 'Show only its turns'}
        </button>
      </div>
      {turn === null ? null : <DisclosureBodies turn={turn} controls={controls} />}
    </div>
  );
}

export interface AgentsTabProps {
  core: CoreState;
  round: number;
  turns: Turn[];
  selected: string | null;
  width: number;
  controls: TranscriptControls;
  onSelect: (id: string) => void;
}

export function AgentsTab({
  core,
  round,
  turns,
  selected,
  width,
  controls,
  onSelect,
}: AgentsTabProps) {
  const graph = useMemo(() => agentGraph(core, round), [core, round]);
  const node =
    graph.nodes.find(candidate => candidate.id === selected) ??
    graph.nodes.find(candidate => candidate.status === 'active') ??
    null;
  const turn = node === null ? null : (turns.find(candidate => candidate.id === node.id) ?? null);
  return (
    <Agents
      round={round}
      graph={graph}
      selected={selected}
      width={width}
      onSelect={onSelect}
      detail={
        <AgentDetail
          node={node}
          turn={turn}
          filtered={node !== null && node.id === selected}
          controls={controls}
          onFilter={() => {
            if (node !== null) onSelect(node.id);
          }}
        />
      }
    />
  );
}
