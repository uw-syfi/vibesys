import {strict as assert} from 'node:assert';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import type {RunEvent} from '@vibesys/backend-client';
import {initialCoreState, reduceEventBatch} from '@vibesys/core-state';
import {renderToStaticMarkup} from 'react-dom/server';
import {openRun, sidebarSections} from '../home.js';
import {runSummary} from '../rounds.js';
import {CAPTURED_TYPES} from '../session.js';
import {Sidebar} from './Sidebar.js';

const LIVE = readFileSync(new URL('../fixtures/demo-run.jsonl', import.meta.url), 'utf8')
  .split('\n')
  .filter(Boolean)
  .map(line => JSON.parse(line) as RunEvent)
  .filter(event => (event.sequence ?? 0) <= 232);

test('the sidebar lists the open run with its rounds, verdict glyphs and deltas', () => {
  const core = reduceEventBatch(initialCoreState(), LIVE);
  const summary = runSummary(
    core,
    LIVE.filter(event => CAPTURED_TYPES.has(event.type)),
    [],
    {
      objective_baseline_value: 950,
    },
  );
  const open = openRun({
    runId: 'run-1',
    title: 'Increase decode throughput',
    project: 'llm-serve',
    status: 'running',
    updatedAt: null,
  });
  const html = renderToStaticMarkup(
    <Sidebar
      width={276}
      sections={sidebarSections([], [], open)}
      current="run-1"
      summary={summary}
      selected={6}
      now={new Date('2026-09-25T14:02:00Z')}
      onRound={() => {}}
    />,
  );
  assert.match(html, /<nav class="side"[^>]*aria-label="Runs"/);
  assert.match(html, /<div class="sect">llm-serve<\/div>/);
  assert.equal(html.match(/aria-current="true"/g)?.length, 1);
  assert.match(html, /aria-current="true"(?:(?!<\/button>).)*>r6</s);
  assert.match(html, /aria-label="reverted"/);
  assert.match(html, /<span class="d">−30<\/span>/);
  assert.match(html, /6 more planned/);
  assert.match(html, /<span class="meta">now<\/span>/);
});

test('the open run says when its project has not attached, in place of planned rounds', () => {
  const summary = runSummary(initialCoreState(), [], [], null);
  const open = openRun({
    runId: 'run-1',
    title: 'Increase decode throughput',
    project: 'llm-serve',
    status: 'running',
    updatedAt: null,
  });
  const html = renderToStaticMarkup(
    <Sidebar
      width={276}
      sections={sidebarSections([], [], open)}
      current="run-1"
      summary={summary}
      note="Waiting for the project to attach"
      selected={null}
      now={new Date('2026-09-25T14:02:00Z')}
      onRound={() => {}}
    />,
  );
  assert.match(html, /<div class="rnd more">Waiting for the project to attach<\/div>/);
});
