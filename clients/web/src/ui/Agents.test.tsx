import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import type {AgentNode} from '../agents.js';
import {AgentDetail, Agents} from './Agents.js';
import type {TranscriptControls} from './Transcript.js';

const CONTROLS: TranscriptControls = {
  expanded: null,
  disclosed: {},
  detail: () => null,
  onExpand: () => {},
  onDisclose: () => {},
};
const NODE: AgentNode = {
  id: '47ae99500678224a4c5e0996413fa3d9',
  role: 'Implementer',
  phase: 'Attempt 1',
  phaseLong: 'Attempt 1',
  status: 'completed',
  label: 'round-1-retry-1-implementer',
  runtime: 'claude-opus-5, Claude Code CLI',
  wall: '8.7s',
  x: 0,
  y: 0,
};

test('the detail names the invocation and offers the filter both ways', () => {
  const html = renderToStaticMarkup(
    <AgentDetail
      node={NODE}
      turn={null}
      filtered={false}
      controls={CONTROLS}
      onFilter={() => {}}
    />,
  );
  assert.match(
    html,
    /<dd class="mono" title="47ae99500678224a4c5e0996413fa3d9">47ae99500678…<\/dd>/,
  );
  assert.match(html, /claude-opus-5, Claude Code CLI/);
  assert.match(html, />Show only its turns</);
  const filtered = renderToStaticMarkup(
    <AgentDetail node={NODE} turn={null} filtered controls={CONTROLS} onFilter={() => {}} />,
  );
  assert.match(filtered, />Show all turns</);
});

test('an empty round says no agent has started', () => {
  const html = renderToStaticMarkup(
    <Agents
      round={2}
      graph={{nodes: [], edges: [], width: 0, height: 0}}
      selected={null}
      width={400}
      onSelect={() => {}}
      detail={null}
    />,
  );
  assert.match(html, /No agent has started in round 2 yet\./);
  assert.match(html, /0 invocations, order inferred/);
});

test('a graph wider than the pane gets a scrolling canvas as wide as the graph', () => {
  const nodes = ['a', 'b', 'c'].map((id, index) => ({...NODE, id, x: 16 + index * 156, y: 16}));
  const html = renderToStaticMarkup(
    <Agents
      round={1}
      graph={{nodes, edges: [], width: 484, height: 76}}
      selected={null}
      width={400}
      onSelect={() => {}}
      detail={null}
    />,
  );
  assert.match(html, /<div class="graphcanvas" style="width:484px;height:76px">/);
});
