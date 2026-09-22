import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import type {GraphColumn} from '../model.js';
import {Graph} from './Graph.js';

const OPUS = 'Claude Code (claude-opus-5)';
const COLUMNS: GraphColumn[] = [
  {
    kind: 'orchestrator',
    nodes: [{id: 'a', role: 'Orchestrator', status: 'completed', runtime: OPUS, count: 2}],
    edge: 'live',
  },
  {
    kind: 'implementer',
    nodes: [
      {
        id: 'c',
        role: 'Implementer',
        status: 'active',
        runtime: 'Codex (gpt-5.1-codex-max)',
        count: 1,
      },
    ],
    edge: 'idle',
  },
  {
    kind: 'judge',
    nodes: [{id: 'd', role: 'Judge', status: 'pending', runtime: null, count: 1}],
    edge: null,
  },
];

test('the graph marks one live node, names role, status and runtime, and takes no focus', () => {
  const html = renderToStaticMarkup(<Graph round={3} columns={COLUMNS} />);
  assert.equal(html.match(/aria-current="step"/g)?.length, 1);
  assert.match(html, /aria-label="Round 3 agents"/);
  assert.match(html, /Implementer.*Active.*Codex \(gpt-5\.1-codex-max\)/s);
  assert.match(html, /Judge.*Pending/s);
  // A collapsed card says how many agents it stands for, in its name as well as on screen.
  assert.match(html, /Orchestrator<span class="gn"> ×2<\/span>/);
  assert.equal(html.includes('×1'), false);
  // The cut runtime keeps its whole text in the one floating tooltip.
  assert.match(html, new RegExp(`data-tip="${OPUS.replace(/[()]/g, '\\$&')}"`));
  // Nodes are a status display: no control, and no runtime row when there is none. The one tab
  // stop is the scrolling row itself, so a keyboard can reach a column that is off screen.
  assert.equal(html.match(/tabindex="0"/g)?.length, 1);
  assert.match(html, /<ol class="gflow"[^>]*tabindex="0"/);
  assert.equal(html.includes('button'), false);
  assert.equal(html.match(/class="grun mono trunc"/g)?.length, 2);
});

test('a round with no agents renders nothing', () => {
  assert.equal(renderToStaticMarkup(<Graph round={3} columns={[]} />), '');
  assert.equal(renderToStaticMarkup(<Graph round={null} columns={COLUMNS} />), '');
});
