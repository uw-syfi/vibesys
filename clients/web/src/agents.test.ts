import {strict as assert} from 'node:assert';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import type {RunEvent} from '@vibesys/backend-client';
import {initialCoreState, reduceEventBatch} from '@vibesys/core-state';
import {agentGraph, inferredEdges} from './agents.js';

const DEMO = readFileSync(new URL('./fixtures/demo-run.jsonl', import.meta.url), 'utf8')
  .split('\n')
  .filter(Boolean)
  .map(line => JSON.parse(line) as RunEvent);
const graphOf = (events: RunEvent[], round: number) =>
  agentGraph(reduceEventBatch(initialCoreState(), events), round);
const ev = (
  sequence: number,
  type: RunEvent['type'],
  fields: Partial<RunEvent> = {},
): RunEvent => ({
  sequence,
  type,
  timestamp: `2026-09-28T12:00:${String(sequence * 5).padStart(2, '0')}Z`,
  ...fields,
});
const phase = (
  sequence: number,
  type: 'phase_started' | 'phase_finished',
  label: string,
  kind: string,
  id?: string,
) =>
  ev(sequence, type, {
    round_label: label,
    agent_kind: kind,
    ...(id === undefined ? {} : {execution_id: id, invocation_id: id}),
    data: {kind: 'phase', phase: kind},
  });

test('a live round: one node per execution, top to bottom, chained by start time', () => {
  const graph = graphOf(
    DEMO.filter(event => (event.sequence ?? 0) <= 232),
    6,
  );
  assert.deepEqual(
    graph.nodes.map(node => [node.role, node.phase, node.status]),
    [
      ['Orchestrator', 'Reviewing', 'completed'],
      ['Orchestrator', 'Planning', 'completed'],
      ['Implementer', 'Attempt 1', 'completed'],
      ['Judge', 'Attempt 1', 'active'],
    ],
  );
  assert.deepEqual(
    graph.edges.map(edge => [edge.source, edge.target]),
    graph.nodes.slice(1).map((node, index) => [graph.nodes[index]?.id, node.id]),
  );
  const ys = graph.nodes.map(node => node.y);
  assert.deepEqual(
    [...ys].sort((left, right) => left - right),
    ys,
  );
  assert.equal(new Set(graph.nodes.map(node => node.x)).size, 1);
  assert.equal(graph.nodes[2]?.runtime, 'claude-opus-5, Claude Code CLI');
  assert.equal(graph.nodes[3]?.wall, 'running');
});

test('a three-way fan-out shares a rank, fed by the execution before it, and outgrows the pane', () => {
  const label = 'round-1-retry-1-implementer';
  const graph = graphOf(
    [
      phase(1, 'phase_started', 'round-1-plan', 'orchestrator', 'p'),
      phase(2, 'phase_finished', 'round-1-plan', 'orchestrator', 'p'),
      phase(3, 'phase_started', label, 'implementer', 'a'),
      phase(4, 'phase_started', label, 'implementer', 'b'),
      phase(5, 'phase_started', label, 'implementer', 'c'),
    ],
    1,
  );
  assert.deepEqual(
    graph.nodes.map(node => node.id),
    ['p', 'a', 'b', 'c'],
  );
  assert.deepEqual(
    graph.edges.map(edge => `${edge.source}>${edge.target}`),
    ['p>a', 'p>b', 'p>c'],
  );
  const [, a, b, c] = graph.nodes;
  assert.equal(new Set([a?.y, b?.y, c?.y]).size, 1);
  assert.equal(new Set([a?.x, b?.x, c?.x]).size, 3);
  // Three 140px cards and their gaps do not fit a 400px pane: the canvas scrolls instead.
  assert.ok(graph.width > 400);
});

test('edges join only executions that did not overlap', () => {
  const span = (start: number, end: number) => ({start, end});
  const edges = inferredEdges([
    {key: 'A', span: span(0, 100)},
    {key: 'B', span: span(1, 2)},
    {key: 'C', span: span(3, 4)},
  ]);
  assert.deepEqual(edges, [['B', 'C']]);
  // A chain keeps only the direct handovers.
  assert.deepEqual(
    inferredEdges([
      {key: 'A', span: span(0, 1)},
      {key: 'B', span: span(2, 3)},
      {key: 'C', span: span(4, 5)},
    ]),
    [
      ['A', 'B'],
      ['B', 'C'],
    ],
  );
});

test('events without execution ids key nodes by kind and label, as transcript turns are', () => {
  const graph = graphOf(
    [
      phase(1, 'phase_started', 'round-1-retry-1-implementer', 'implementer'),
      phase(2, 'phase_finished', 'round-1-retry-1-implementer', 'implementer'),
      phase(3, 'phase_started', 'round-1-retry-2-implementer', 'implementer'),
      phase(4, 'phase_finished', 'round-1-retry-2-implementer', 'implementer'),
      phase(5, 'phase_started', 'round-1-retry-2-judge', 'judge'),
    ],
    1,
  );
  // core-state keeps one slot per kind and round for executions without ids, so the second
  // attempt's start overwrites the first: one node, keyed like the transcript turn it names.
  // core-state id-less phase slots (follow-up; see docs/superpowers/follow-ups.md).
  assert.deepEqual(
    graph.nodes.map(node => node.id),
    ['implementer|round-1-retry-2-implementer', 'judge|round-1-retry-2-judge'],
  );
  assert.equal(graph.edges.length, 1);
});

test('a round with no started execution has an empty graph', () => {
  assert.deepEqual(graphOf([], 1), {nodes: [], edges: [], width: 0, height: 0});
});
