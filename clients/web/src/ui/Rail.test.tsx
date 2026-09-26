import {strict as assert} from 'node:assert';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import type {RunEvent} from '@vibesys/backend-client/browser';
import {initialCoreState, reduceEventBatch} from '@vibesys/core-state';
import {renderToStaticMarkup} from 'react-dom/server';
import {railModel} from '../derive.js';
import {Rail} from './Rail.js';
import {Summary} from './Summary.js';

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
      onSelect={() => {}}
      onRetry={() => {}}
    />,
  );
  assert.match(html, /<span class="sr-only">R1, running<\/span>/);
  assert.match(html, /<span class="val mono"[^>]*aria-hidden="true"/);
});

test('the summary names the metric and the best kept round against the baseline', () => {
  const html = renderToStaticMarkup(
    <Summary
      model={{
        metric: 'ops_per_sec',
        unit: 'ops/s',
        direction: 'max',
        baseline: '900',
        best: {value: '1,315', round: 8, delta: '+46.1%', improved: true},
        done: 8,
        max: 20,
        kept: 5,
      }}
      trend={{
        points: [
          {x: 3, y: 32},
          {x: 50, y: 18},
          {x: 97, y: 4},
        ],
        first: '900',
        last: '1,315',
        firstRound: 0,
        lastRound: 8,
      }}
    />,
  );
  assert.match(html, /<span class="mono">ops_per_sec<\/span>/);
  assert.match(html, /<span class="delta up"[^>]*>\+46\.1%<\/span>/);
  assert.match(html, / · R8/);
  assert.match(html, /aria-label="Metric trend: 900 to 1,315, R0 to R8"/);
  assert.match(html, /<polyline points="3,32 50,18 97,4"><\/polyline>/);
  // A picture, not a control: the plot takes no focus.
  assert.equal(/tabindex/.test(html), false);
});
