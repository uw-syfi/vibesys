import {strict as assert} from 'node:assert';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import type {RunEvent} from '@vibesys/backend-client/browser';
import {initialCoreState, reduceEventBatch} from '@vibesys/core-state';
import {renderToStaticMarkup} from 'react-dom/server';
import {railModel} from '../derive.js';
import {Rail} from './Rail.js';

const STUB = readFileSync(new URL('../fixtures/stub-run.jsonl', import.meta.url), 'utf8')
  .split('\n')
  .filter(Boolean)
  .map(line => JSON.parse(line) as RunEvent);

test('the running row is named by its status; the ticking elapsed stays out of the name', () => {
  // The stub run at 60: round 1 is running.
  const core = reduceEventBatch(
    initialCoreState(),
    STUB.filter(event => (event.sequence ?? 0) <= 60),
  );
  const html = renderToStaticMarkup(
    <Rail
      state="ready"
      model={railModel(core, [], null)}
      selected={1}
      error={null}
      hint={false}
      trend={null}
      onSelect={() => {}}
      onRetry={() => {}}
    />,
  );
  assert.match(html, /<span class="sr-only">R1, running<\/span>/);
  assert.match(html, /<span class="val mono"[^>]*aria-hidden="true"/);
  assert.equal(html.includes('tplot'), false, 'no series, no sparkline');
});

test('the trend is one named polyline and a dot; it repeats no row and takes no focus', () => {
  const core = reduceEventBatch(initialCoreState(), STUB);
  const html = renderToStaticMarkup(
    <Rail
      state="ready"
      model={railModel(core, [], null)}
      selected={8}
      error={null}
      hint={false}
      trend={{
        points: [
          {x: 3, y: 32},
          {x: 50, y: 18},
          {x: 97, y: 4},
        ],
        first: '900',
        last: '1.315K',
        firstRound: 0,
        lastRound: 8,
      }}
      onSelect={() => {}}
      onRetry={() => {}}
    />,
  );
  assert.match(html, /<svg class="tplot"[^>]*role="img"/);
  assert.match(html, /aria-label="Metric trend: 900 to 1\.315K, R0 to R8"/);
  assert.match(html, /<polyline points="3,32 50,18 97,4"><\/polyline>/);
  assert.match(html, /<path class="tdot" d="M97,4h0"><\/path>/);
  // The endpoints are the plot's own scale, and its name already said them: announce once.
  assert.match(html, /<p class="tends mono" aria-hidden="true">/);
  // The rail's one Tab stop is the selected row; the plot is a picture, not a control.
  assert.equal(/<svg class="tplot"[^>]*tabindex/.test(html), false);
  assert.equal(html.match(/tabindex="0"/g)?.length, 1);
});
