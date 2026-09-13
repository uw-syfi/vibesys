import {describe, expect, test} from 'bun:test';
import type {AgentPhase} from '@vibesys/core-state';
import {
  type AgentGraph,
  type GraphCell,
  type GraphNode,
  graphPaneBounds,
  layoutAgentGraph,
  NODE_HEIGHT,
  shadowCells,
  stageKinds,
} from './agent-graph.js';

function phase(kind: string, status: AgentPhase['status'], roundNumber = 1): AgentPhase {
  return {kind, status, roundNumber, roundLabel: `round-${roundNumber}-${kind}`};
}

const CHAIN = [
  phase('orchestrator', 'completed'),
  phase('implementer', 'active'),
  phase('judge', 'pending'),
];

/** The width of a node's label when it is selected, as the agent map draws it. */
const selectedLabel = (item: AgentPhase): number => `› ● ${item.kind}`.length;

describe('stageKinds', () => {
  test('keeps the order the round first mentions each kind', () => {
    expect(stageKinds(CHAIN)).toEqual(['orchestrator', 'implementer', 'judge']);
  });

  test('collapses repeats of a kind into one stage', () => {
    const parallel = [phase('implementer', 'active'), phase('implementer', 'completed')];
    expect(stageKinds(parallel)).toEqual(['implementer']);
  });
});

describe('layoutAgentGraph', () => {
  test('places one column per stage, left to right', () => {
    const graph = layoutAgentGraph(CHAIN, graphPaneBounds(CHAIN).max - 4);
    const xs = graph.nodes.map(node => node.x);
    expect(xs).toEqual([...xs].sort((a, b) => a - b));
    expect(new Set(xs).size).toBe(3);
    expect(graph.nodes.every(node => node.y === 0)).toBe(true);
  });

  test('stacks several agents of one kind inside their column', () => {
    const parallel = [
      phase('orchestrator', 'completed'),
      phase('implementer', 'active'),
      phase('implementer', 'active'),
    ];
    const graph = layoutAgentGraph(parallel, graphPaneBounds(parallel).max - 4);
    const implementers = graph.nodes.filter(node => node.phase.kind === 'implementer');
    expect(implementers).toHaveLength(2);
    expect(implementers[0]?.x).toBe(implementers[1]?.x as number);
    expect((implementers[1]?.y as number) - (implementers[0]?.y as number)).toBeGreaterThanOrEqual(
      NODE_HEIGHT,
    );
  });

  test('centres a shorter stage against the tallest one', () => {
    const parallel = [
      phase('orchestrator', 'completed'),
      phase('implementer', 'active'),
      phase('implementer', 'active'),
    ];
    const graph = layoutAgentGraph(parallel, graphPaneBounds(parallel).max - 4);
    const orchestrator = graph.nodes.find(node => node.phase.kind === 'orchestrator');
    expect(orchestrator?.y).toBeGreaterThan(0);
  });

  test('draws an arrow into every target and keeps edges inside the gutter', () => {
    const graph = layoutAgentGraph(CHAIN, graphPaneBounds(CHAIN).max - 4);
    const arrows = graph.cells.filter(cell => cell.glyph === '▶');
    expect(arrows).toHaveLength(2);
    const nodeWidth = graph.nodes[0]?.width as number;
    for (const cell of graph.cells) {
      const column = cell.x % (nodeWidth + 5);
      expect(column).toBeGreaterThanOrEqual(nodeWidth);
    }
  });

  test('tones an edge by the phases it connects', () => {
    const four = [...CHAIN, phase('profiler', 'pending')];
    const graph = layoutAgentGraph(four, graphPaneBounds(four).max - 4);
    // Live means data has flowed: a completed source feeding an active
    // target. A stage that has not produced anything yet, or that feeds a
    // stage that has not started, stays idle.
    expect(graph.cells.some(cell => cell.tone === 'live')).toBe(true);
    expect(graph.cells.some(cell => cell.tone === 'idle')).toBe(true);
  });

  test('an edge from an active source to a pending target is idle, not live', () => {
    // The active node has not produced anything yet, so its outbound edge
    // must not be painted as live dataflow.
    const activeToPending = [phase('implementer', 'active'), phase('judge', 'pending')];
    const graph = layoutAgentGraph(activeToPending, graphPaneBounds(activeToPending).max - 4);
    expect(graph.cells.every(cell => cell.tone === 'idle')).toBe(true);
  });

  test('an edge from a completed source to an active target is still live', () => {
    const completedToActive = [phase('implementer', 'completed'), phase('judge', 'active')];
    const graph = layoutAgentGraph(completedToActive, graphPaneBounds(completedToActive).max - 4);
    expect(graph.cells.every(cell => cell.tone === 'live')).toBe(true);
  });

  test.each([
    'failed',
    'cancelled',
    'interrupted',
  ] as const)('a %s source still fails the edge, even into an active target', status => {
    const badSource = [phase('implementer', status), phase('judge', 'active')];
    const graph = layoutAgentGraph(badSource, graphPaneBounds(badSource).max - 4);
    expect(graph.cells.every(cell => cell.tone === 'failed')).toBe(true);
  });

  test('a completed source into any non-pending target still reads as done', () => {
    const completedToFailed = [phase('implementer', 'completed'), phase('judge', 'failed')];
    const graph = layoutAgentGraph(completedToFailed, graphPaneBounds(completedToFailed).max - 4);
    expect(graph.cells.every(cell => cell.tone === 'done')).toBe(true);
  });

  test('an active fan-out node leaves every outbound edge idle', () => {
    const fanOut = [
      phase('orchestrator', 'active'),
      phase('implementer', 'pending'),
      phase('implementer', 'pending'),
      phase('implementer', 'pending'),
    ];
    const graph = layoutAgentGraph(fanOut, graphPaneBounds(fanOut).max - 4);
    expect(graph.cells.every(cell => cell.tone === 'idle')).toBe(true);
  });

  test('an active fan-in node has every completed-source inbound edge live', () => {
    const fanIn = [
      phase('implementer', 'completed'),
      phase('implementer', 'completed'),
      phase('implementer', 'completed'),
      phase('judge', 'active'),
    ];
    const graph = layoutAgentGraph(fanIn, graphPaneBounds(fanIn).max - 4);
    expect(graph.cells.every(cell => cell.tone === 'live')).toBe(true);
  });

  test('a finished handover between finished stages reads as done', () => {
    const done = [phase('implementer', 'completed'), phase('judge', 'completed')];
    const graph = layoutAgentGraph(done, graphPaneBounds(done).max - 4);
    expect(graph.cells.every(cell => cell.tone === 'done')).toBe(true);
  });

  test('a failed phase colors the edge leaving it', () => {
    const failed = [phase('implementer', 'failed'), phase('judge', 'pending')];
    const graph = layoutAgentGraph(failed, graphPaneBounds(failed).max - 4);
    expect(graph.cells.every(cell => cell.tone === 'failed')).toBe(true);
  });

  test('gives overlapping fan-outs their own lanes', () => {
    const fan = [
      phase('orchestrator', 'completed'),
      phase('implementer', 'active'),
      phase('implementer', 'active'),
      phase('implementer', 'active'),
    ];
    const graph = layoutAgentGraph(fan, graphPaneBounds(fan).max - 4);
    // Every implementer must be reachable: one arrow head each.
    expect(graph.cells.filter(cell => cell.glyph === '▶')).toHaveLength(3);
  });

  test('narrow panes shrink the node, never below the readable floor', () => {
    const wide = layoutAgentGraph(CHAIN, graphPaneBounds(CHAIN).max - 4);
    const narrow = layoutAgentGraph(CHAIN, graphPaneBounds(CHAIN).min - 4);
    expect(narrow.nodes[0]?.width).toBeLessThan(wide.nodes[0]?.width as number);
    expect(narrow.nodes[0]?.width).toBeGreaterThanOrEqual(14);
  });

  test('gives a long name the columns a short one does not need', () => {
    // 56 columns split evenly are three nodes of 15, one short of `✓ orchestrator`,
    // while `judge` leaves six of its own unused.
    const graph = layoutAgentGraph(CHAIN, 56, selectedLabel);
    expect(graph.nodes.map(node => node.width)).toEqual([16, 16, 14]);
    expect(graph.width).toBeLessThanOrEqual(56);
    // Every edge still ends on the next node's border.
    const heads = graph.cells.filter(cell => cell.glyph === '▶').map(cell => cell.x);
    expect(heads).toEqual(graph.nodes.slice(1).map(node => node.x - 1));
  });

  test('keeps the columns even while every name fits them', () => {
    const graph = layoutAgentGraph(CHAIN, graphPaneBounds(CHAIN).max - 4, selectedLabel);
    expect(graph.nodes.map(node => node.width)).toEqual([18, 18, 18]);
  });

  test('handles an empty round without throwing', () => {
    const graph = layoutAgentGraph([], 60);
    expect(graph.nodes).toEqual([]);
    expect(graph.cells).toEqual([]);
    expect(graph.width).toBe(0);
  });
});

describe('graphPaneBounds', () => {
  test('grows with the number of stages', () => {
    const four = [...CHAIN, phase('profiler', 'pending')];
    expect(graphPaneBounds(four).min).toBeGreaterThan(graphPaneBounds(CHAIN).min);
    expect(graphPaneBounds(four).max).toBeGreaterThan(graphPaneBounds(four).min);
  });

  test('its floor is the narrowest pane that holds every label in full', () => {
    // `performance_profiler` is wider than a node at its widest even split.
    const long = [phase('performance_profiler', 'active'), phase('judge', 'pending')];
    for (const phases of [CHAIN, long]) {
      const {min, max} = graphPaneBounds(phases, selectedLabel);
      const whole = (paneWidth: number): boolean =>
        layoutAgentGraph(phases, paneWidth - 4, selectedLabel).nodes.every(
          node => node.width - 2 >= selectedLabel(node.phase),
        );
      expect({kinds: stageKinds(phases), atFloor: whole(min), below: whole(min - 1)}).toEqual({
        kinds: stageKinds(phases),
        atFloor: true,
        below: false,
      });
      expect(max).toBeGreaterThanOrEqual(min);
    }
  });
});

describe('a round with many agents', () => {
  const many: AgentPhase[] = [
    phase('orchestrator', 'completed'),
    ...Array.from({length: 4}, () => phase('implementer', 'active')),
    ...Array.from({length: 3}, () => phase('judge', 'pending')),
    ...Array.from({length: 2}, () => phase('profiler', 'pending')),
  ];

  test('gives every agent its own node and every fed agent an arrow', () => {
    const graph = layoutAgentGraph(many, graphPaneBounds(many).max - 4);
    expect(graph.nodes).toHaveLength(10);
    // One arrow head per node that is fed, not one per edge: several sources
    // converging on a judge share the head they point at.
    expect(graph.cells.filter(cell => cell.glyph === '▶')).toHaveLength(4 + 3 + 2);
  });

  test('never stacks two nodes on the same cell', () => {
    const graph = layoutAgentGraph(many, graphPaneBounds(many).max - 4);
    const seen = new Set<string>();
    for (const node of graph.nodes) {
      for (let row = node.y; row < node.y + NODE_HEIGHT; row += 1) {
        const key = `${node.x},${row}`;
        expect(seen.has(key)).toBe(false);
        seen.add(key);
      }
    }
  });

  test('keeps edge cells out of the columns the nodes occupy', () => {
    const graph = layoutAgentGraph(many, graphPaneBounds(many).max - 4);
    const width = graph.nodes[0]?.width as number;
    for (const cell of graph.cells) {
      expect(cell.x % (width + 5)).toBeGreaterThanOrEqual(width);
    }
  });

  test('resolves crossing edges into junctions rather than overwriting', () => {
    const graph = layoutAgentGraph(many, graphPaneBounds(many).max - 4);
    const junctions = graph.cells.filter(cell => '┼├┤┬┴'.includes(cell.glyph));
    expect(junctions.length).toBeGreaterThan(0);
  });
});

/**
 * The drop shadow the round view draws for the selected node
 * (`agent-map.ts#renderGraph`). Pure geometry, like the rest of this module:
 * colour and glyph choice belong to the renderer.
 */
describe('shadowCells', () => {
  function graphOf(
    nodes: GraphNode[],
    cells: GraphCell[],
    width: number,
    height: number,
  ): AgentGraph {
    return {nodes, cells, width, height};
  }

  /** Bounds with slack on every side: the common case, nothing clipped. */
  const ROOMY = {width: 100, height: 100};

  test('draws the right column and the bottom row, sharing one corner cell', () => {
    const node: GraphNode = {phase: phase('implementer', 'active'), x: 0, y: 0, width: 10};
    const graph = graphOf([node], [], 30, 30);
    const cells = shadowCells(graph, node, ROOMY);

    // Right column: x = 10 (one past the border), rows 1..NODE_HEIGHT.
    for (let y = 1; y < NODE_HEIGHT; y += 1) {
      expect(cells).toContainEqual({x: 10, y, edge: 'right'});
    }
    // Bottom row: y = NODE_HEIGHT (one past the border), columns 1..width.
    for (let x = 1; x < 10; x += 1) {
      expect(cells).toContainEqual({x, y: NODE_HEIGHT, edge: 'bottom'});
    }
    // The shared corner, (10, NODE_HEIGHT), reads `bottom` and appears once.
    const corner = cells.filter(cell => cell.x === 10 && cell.y === NODE_HEIGHT);
    expect(corner).toEqual([{x: 10, y: NODE_HEIGHT, edge: 'bottom'}]);
    // NODE_HEIGHT - 1 plain right-column cells, plus the whole bottom row
    // (node.width cells, corner included).
    expect(cells).toHaveLength(NODE_HEIGHT - 1 + 10);
  });

  test('skips a cell already carrying an edge or an arrowhead', () => {
    const node: GraphNode = {phase: phase('implementer', 'active'), x: 0, y: 0, width: 10};
    const edgeCell: GraphCell = {x: 10, y: 1, glyph: '─', tone: 'idle'};
    const arrowCell: GraphCell = {x: 10, y: 3, glyph: '▶', tone: 'idle'};
    const graph = graphOf([node], [edgeCell, arrowCell], 30, 30);
    const cells = shadowCells(graph, node, ROOMY);

    expect(cells.some(cell => cell.x === 10 && cell.y === 1)).toBe(false);
    expect(cells.some(cell => cell.x === 10 && cell.y === 3)).toBe(false);
    // An untouched row in the same column still draws.
    expect(cells.some(cell => cell.x === 10 && cell.y === 2)).toBe(true);
  });

  test('skips a cell that sits inside another node', () => {
    const node: GraphNode = {phase: phase('implementer', 'active'), x: 0, y: 0, width: 10};
    const neighbor: GraphNode = {phase: phase('judge', 'pending'), x: 10, y: 1, width: 8};
    const graph = graphOf([node, neighbor], [], 30, 30);
    const cells = shadowCells(graph, node, ROOMY);

    // (10, 1) is the shadow's own departure-row cell and sits inside
    // `neighbor`'s rectangle; it must not draw a shadow over it.
    expect(cells.some(cell => cell.x === 10 && cell.y === 1)).toBe(false);
  });

  test('skips a cell past the bounds the caller hands in, standing in for the pane border', () => {
    const node: GraphNode = {phase: phase('judge', 'pending'), x: 0, y: 0, width: 10};
    const graph = graphOf([node], [], 30, 30);
    // Exactly the node's own footprint: nothing reserved past it, on either
    // side, for a shadow to land in.
    const cells = shadowCells(graph, node, {width: 10, height: NODE_HEIGHT});

    expect(cells).toEqual([]);
  });

  test('draws in full once bounds give it room, even against a graph sized tight to its nodes', () => {
    // `graph.width`/`.height` alone are not the safety bound (that is the
    // point of this test): a graph exactly the size of its one node still
    // gets a full shadow once `bounds` has slack past it.
    const node: GraphNode = {phase: phase('judge', 'pending'), x: 0, y: 0, width: 10};
    const graph = graphOf([node], [], 10, NODE_HEIGHT);
    const cells = shadowCells(graph, node, ROOMY);

    expect(cells.some(cell => cell.edge === 'right')).toBe(true);
    expect(cells.some(cell => cell.edge === 'bottom')).toBe(true);
  });

  test('the last stage of a real layout has no gutter past it, so no shadow without slack', () => {
    const graph = layoutAgentGraph(CHAIN, graphPaneBounds(CHAIN).max - 4);
    const last = graph.nodes.at(-1) as GraphNode;
    // Bounds sized to exactly what the graph itself needs, on every side: the
    // pane is drawn at its floor, no bigger than the graph requires. Neither
    // the right column (no gutter past the last stage) nor the bottom row
    // (this chain's one row is already the tallest column) has anywhere left
    // to draw.
    const tight = shadowCells(graph, last, {width: graph.width, height: graph.height});
    expect(tight).toEqual([]);

    // The same node, given a pane with slack past the graph on both axes,
    // gets the full shadow: this is a property of the room on hand, not of
    // being the last stage.
    const roomy = shadowCells(graph, last, ROOMY);
    expect(roomy.some(cell => cell.edge === 'right')).toBe(true);
    expect(roomy.some(cell => cell.edge === 'bottom')).toBe(true);
  });

  test("a middle stage of a real layout loses only its edge's departure cell", () => {
    const graph = layoutAgentGraph(CHAIN, graphPaneBounds(CHAIN).max - 4);
    const middle = graph.nodes[1] as GraphNode; // 'implementer': fed and feeding.
    const cells = shadowCells(graph, middle, ROOMY);
    const rightX = middle.x + middle.width;
    const departureRow = middle.y + 1;

    expect(graph.cells.some(cell => cell.x === rightX && cell.y === departureRow)).toBe(true);
    expect(cells.some(cell => cell.x === rightX && cell.y === departureRow)).toBe(false);
    for (let y = middle.y + 2; y <= middle.y + NODE_HEIGHT; y += 1) {
      expect(cells.some(cell => cell.x === rightX && cell.y === y)).toBe(true);
    }
  });
});

/**
 * A regression guard for the drop-shadow change: `shadowCells` is additive,
 * and neither `layoutAgentGraph` nor `routeEdges` changed alongside it, so a
 * small graph's node bounds and edge cells must still match what they were
 * before that change. Captured from this same call against the pre-shadow
 * code.
 */
describe('layout stability under the drop-shadow addition', () => {
  test('node bounds and edge cells for a 4-node graph are unchanged', () => {
    const four = [...CHAIN, phase('profiler', 'pending')];
    const graph = layoutAgentGraph(four, graphPaneBounds(four).max - 4);

    expect(graph.nodes.map(node => ({x: node.x, y: node.y, width: node.width}))).toEqual([
      {x: 0, y: 0, width: 18},
      {x: 23, y: 0, width: 18},
      {x: 46, y: 0, width: 18},
      {x: 69, y: 0, width: 18},
    ]);
    expect(graph.width).toBe(87);
    expect(graph.height).toBe(5);
    expect(graph.cells).toEqual([
      {x: 18, y: 1, glyph: '─', tone: 'live'},
      {x: 19, y: 1, glyph: '─', tone: 'live'},
      {x: 20, y: 1, glyph: '─', tone: 'live'},
      {x: 21, y: 1, glyph: '─', tone: 'live'},
      {x: 22, y: 1, glyph: '▶', tone: 'live'},
      {x: 41, y: 1, glyph: '─', tone: 'live'},
      {x: 42, y: 1, glyph: '─', tone: 'live'},
      {x: 43, y: 1, glyph: '─', tone: 'live'},
      {x: 44, y: 1, glyph: '─', tone: 'live'},
      {x: 45, y: 1, glyph: '▶', tone: 'live'},
      {x: 64, y: 1, glyph: '─', tone: 'idle'},
      {x: 65, y: 1, glyph: '─', tone: 'idle'},
      {x: 66, y: 1, glyph: '─', tone: 'idle'},
      {x: 67, y: 1, glyph: '─', tone: 'idle'},
      {x: 68, y: 1, glyph: '▶', tone: 'idle'},
    ]);
  });
});
