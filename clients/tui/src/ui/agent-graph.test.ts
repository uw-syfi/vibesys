import {describe, expect, test} from 'bun:test';
import type {AgentPhase} from '@vibesys/core-state';
import {graphPaneBounds, layoutAgentGraph, NODE_HEIGHT, stageKinds} from './agent-graph.js';

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
    // The frontier glows: an edge is live while either end is running. Two
    // stages that have not run yet stay idle.
    expect(graph.cells.some(cell => cell.tone === 'live')).toBe(true);
    expect(graph.cells.some(cell => cell.tone === 'idle')).toBe(true);
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
