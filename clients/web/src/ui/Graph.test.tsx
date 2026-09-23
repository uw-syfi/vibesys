import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import type {AgentGraph} from '../model.js';
import {Graph} from './Graph.js';

const OPUS = 'Claude Code (claude-opus-5)';
const GRAPH: AgentGraph = {
  nodes: [
    {
      id: 'a',
      role: 'Orchestrator',
      status: 'completed',
      runtime: 'claude-opus-5',
      runtimeTip: OPUS,
      count: 2,
    },
    {
      id: 'c',
      role: 'Implementer',
      status: 'active',
      runtime: 'gpt-5.1-codex-max',
      runtimeTip: null,
      count: 1,
    },
    {id: 'd', role: 'Judge', status: 'pending', runtime: 'Stub', runtimeTip: null, count: 1},
    {id: 'e', role: 'Profiler', status: 'pending', runtime: null, runtimeTip: null, count: 1},
  ],
  edges: [
    {from: 'a', to: 'c', tone: 'live'},
    {from: 'c', to: 'd', tone: 'idle'},
    {from: 'd', to: 'e', tone: 'idle'},
  ],
};

test('the graph marks one live node, names role, status and runtime, and takes no focus', () => {
  const html = renderToStaticMarkup(<Graph round={3} graph={GRAPH} />);
  assert.equal(html.match(/aria-current="step"/g)?.length, 1);
  assert.match(html, /aria-label="Round 3 agents"/);
  // The live agent reads "Running", as the rail and the live region call it.
  assert.match(html, /Implementer.*Running.*gpt-5\.1-codex-max/s);
  assert.equal(html.includes('Active'), false);
  assert.match(html, /Judge.*Pending/s);
  // A collapsed card says how many agents it stands for on screen, and in words for a reader
  // that drops the multiplication sign.
  assert.match(html, /<span class="sr-only"> 2 runs<\/span>/);
  assert.match(html, /<span class="gn" aria-hidden="true"> ×2<\/span>/);
  assert.equal(html.includes('×1'), false);
  assert.equal(html.includes('1 runs'), false);
  // Nodes are a status display: no control anywhere, and nothing focusable but the row.
  assert.equal(html.includes('button'), false);
  assert.equal(html.match(/tabindex/g)?.length, 1);
});

test('the cards sit where the layout put them, in loop order whatever it decided', () => {
  const html = renderToStaticMarkup(<Graph round={3} graph={GRAPH} />);
  const roles = [...html.matchAll(/class="grole trunc">([A-Za-z ]+)/g)].map(match => match[1]);
  assert.deepEqual(roles, ['Orchestrator', 'Implementer', 'Judge', 'Profiler']);
  // Every card is absolutely placed at its own box, all of them the one card size.
  const boxes = [...html.matchAll(/class="gnode"[^>]*style="([^"]*)"/g)].map(match => match[1]);
  assert.equal(boxes.length, 4);
  assert.equal(new Set(boxes).size, 4, 'no two cards share a position');
  for (const box of boxes) assert.match(box ?? '', /width:160px;height:44px/);
});

test('every edge is one path, toned by its two ends, with an arrow head', () => {
  const html = renderToStaticMarkup(<Graph round={3} graph={GRAPH} />);
  const tones = [...html.matchAll(/class="gedge" data-tone="(\w+)"/g)].map(match => match[1]);
  assert.deepEqual(tones, ['live', 'idle', 'idle']);
  assert.equal(html.match(/marker-end="url\(#gtip-(live|idle)\)"/g)?.length, 3);
  // The whole edge layer is decoration: the list of cards carries the round's shape.
  assert.match(html, /<svg class="gedges"[^>]*aria-hidden="true"/);
  assert.match(html, /d="M[\d.]+,[\d.]+/);
});

test('the row is a tab stop only once it has somewhere to scroll', () => {
  // Server-rendered, so nothing is measured yet: the row is not in the tab order.
  const html = renderToStaticMarkup(<Graph round={3} graph={GRAPH} />);
  assert.match(html, /<div class="gflow"[^>]*tabindex="-1"/);
  assert.equal(html.includes('data-more'), false);
});

test('the runtime line shows the model; the harness rides in the tooltip and the name', () => {
  const html = renderToStaticMarkup(<Graph round={3} graph={GRAPH} />);
  const opus = 'Claude Code \\(claude-opus-5\\)';
  // Model and harness: the card shows the model, the pair is the tooltip and what is announced.
  assert.match(
    html,
    new RegExp(
      `<span class="sr-only">${opus}</span><span class="grun mono trunc" data-tip="${opus}" ` +
        'aria-hidden="true">claude-opus-5</span>',
    ),
  );
  // Model only, and harness only: the card says it all, so no tooltip and nothing to hide.
  assert.match(html, /<span class="grun mono trunc">gpt-5\.1-codex-max<\/span>/);
  assert.match(html, /<span class="grun mono trunc">Stub<\/span>/);
  // Neither: no runtime line at all, so three of the four cards have one.
  assert.equal(html.match(/class="grun mono trunc"/g)?.length, 3);
  assert.equal(html.match(/data-tip=/g)?.length, 1);
});

test('a round with no agents renders nothing', () => {
  assert.equal(renderToStaticMarkup(<Graph round={3} graph={{nodes: [], edges: []}} />), '');
  assert.equal(renderToStaticMarkup(<Graph round={null} graph={GRAPH} />), '');
});
