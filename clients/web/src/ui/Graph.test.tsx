import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import type {GraphColumn} from '../model.js';
import {Graph} from './Graph.js';

const OPUS = 'Claude Code (claude-opus-5)';
const COLUMNS: GraphColumn[] = [
  {
    kind: 'orchestrator',
    nodes: [
      {
        id: 'a',
        role: 'Orchestrator',
        status: 'completed',
        runtime: 'claude-opus-5',
        runtimeTip: OPUS,
        count: 2,
      },
    ],
    edge: 'live',
  },
  {
    kind: 'implementer',
    nodes: [
      {
        id: 'c',
        role: 'Implementer',
        status: 'active',
        runtime: 'gpt-5.1-codex-max',
        runtimeTip: null,
        count: 1,
      },
    ],
    edge: 'idle',
  },
  {
    kind: 'judge',
    nodes: [
      {id: 'd', role: 'Judge', status: 'pending', runtime: 'Stub', runtimeTip: null, count: 1},
    ],
    edge: 'idle',
  },
  {
    kind: 'profiler',
    nodes: [
      {id: 'e', role: 'Profiler', status: 'pending', runtime: null, runtimeTip: null, count: 1},
    ],
    edge: null,
  },
];

test('the graph marks one live node, names role, status and runtime, and takes no focus', () => {
  const html = renderToStaticMarkup(<Graph round={3} columns={COLUMNS} />);
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

test('the row is a tab stop only once it has somewhere to scroll', () => {
  // Server-rendered, so nothing is measured yet: the row is not in the tab order.
  const html = renderToStaticMarkup(<Graph round={3} columns={COLUMNS} />);
  assert.match(html, /<ol class="gflow"[^>]*tabindex="-1"/);
  assert.equal(html.includes('data-more'), false);
});

test('the runtime line shows the model; the harness rides in the tooltip and the name', () => {
  const html = renderToStaticMarkup(<Graph round={3} columns={COLUMNS} />);
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
  // Neither: no runtime line at all, so three of the four columns have one.
  assert.equal(html.match(/class="grun mono trunc"/g)?.length, 3);
  assert.equal(html.match(/data-tip=/g)?.length, 1);
});

test('a round with no agents renders nothing', () => {
  assert.equal(renderToStaticMarkup(<Graph round={3} columns={[]} />), '');
  assert.equal(renderToStaticMarkup(<Graph round={null} columns={COLUMNS} />), '');
});
