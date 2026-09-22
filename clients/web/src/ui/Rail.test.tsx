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
      onSelect={() => {}}
      onRetry={() => {}}
    />,
  );
  assert.match(html, /<span class="sr-only">R1, running<\/span>/);
  assert.match(html, /<span class="val mono"[^>]*aria-hidden="true"/);
});
