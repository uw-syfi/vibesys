import {strict as assert} from 'node:assert';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import type {RunEvent} from '@vibesys/backend-client';
import {initialCoreState, reduceEventBatch} from '@vibesys/core-state';
import {renderToStaticMarkup} from 'react-dom/server';
import {resultParts, runSummary} from '../rounds.js';
import {CAPTURED_TYPES} from '../session.js';
import {type RoundTranscript, roundTranscript, toolDetail} from '../transcript.js';
import {Transcript, type TranscriptControls, type TranscriptProps} from './Transcript.js';

const DEMO = readFileSync(new URL('../fixtures/demo-run.jsonl', import.meta.url), 'utf8')
  .split('\n')
  .filter(Boolean)
  .map(line => JSON.parse(line) as RunEvent);
const core = reduceEventBatch(initialCoreState(), DEMO);
const captured = DEMO.filter(event => CAPTURED_TYPES.has(event.type));
const model = roundTranscript({core, captured, sent: [], round: 1, runId: null});
const summary = runSummary(core, captured, [], null);
const row = summary.rows[0] ?? null;
const controls = (
  expanded: string | null = null,
  disclosed: Record<string, boolean> = {},
): TranscriptControls => ({
  expanded,
  disclosed,
  detail: id => toolDetail(core, id),
  onExpand: () => {},
  onDisclose: () => {},
});
const render = (props: Partial<TranscriptProps> = {}) =>
  renderToStaticMarkup(
    <Transcript
      round={1}
      row={row}
      result={row === null ? [] : resultParts(row, summary.unit, false)}
      model={model}
      follow={false}
      history={{loading: false, error: null, onRetry: () => {}}}
      empty="Waiting for round 1."
      endline={null}
      controls={controls()}
      only={null}
      onShowAll={() => {}}
      {...props}
    />,
  );

test('round 1: collapsed one-line tool rows, the judge analysis, then its verdict', () => {
  const html = render();
  assert.match(html, /<span class="rn">Round 1<\/span>/);
  assert.match(html, />Accepted</);
  assert.match(
    html,
    /<span class="verb">Searched<\/span><span class="obj">src\/batch.rs<\/span><span class="sub">for fn decode_step<\/span>/,
  );
  assert.match(html, /<span class="bad">exit 1<\/span>/);
  assert.match(html, /aria-label="Judge verdict"/);
  assert.equal(html.includes('aria-expanded="true"'), false);
});

test('an expanded edit shows its diff; an expanded command shows its output', () => {
  assert.match(render({controls: controls('26')}), /<div class="del">/);
  const output = render({controls: controls('28')});
  assert.match(output, /<span class="cmd">\$ cargo test --release/);
  assert.match(output, /<span class="fl">/);
});

test('queued steers wait under the live round', () => {
  const queued: RoundTranscript = {
    ...model,
    queued: [{id: 'sent-1', text: 'Measure lock hold time first.'}],
  };
  assert.match(render({model: queued}), /Queued for the next agent call/);
});

test('the agent filter shows one turn and says so', () => {
  const implementer = model.turns.find(turn => turn.kind === 'implementer');
  const html = render({only: implementer?.id ?? null});
  assert.match(html, /Showing only Implementer \(attempt 1\)/);
  assert.equal(html.match(/<section class="turn"/g)?.length, 1);
});

test('a prompt is offered, and shown once disclosed', () => {
  const first = model.turns[0];
  assert.ok(first);
  const withPrompt: RoundTranscript = {
    ...model,
    turns: [{...first, prompt: 'Review round 1.'}, ...model.turns.slice(1)],
  };
  assert.match(render({model: withPrompt}), /aria-expanded="false"[^>]*>.*Prompt</s);
  const open = render({
    model: withPrompt,
    controls: controls(null, {[`${first.id}:prompt`]: true}),
  });
  assert.match(open, /Prompt sent to orchestrator/);
  assert.match(open, /<pre>Review round 1.<\/pre>/);
});

test('no round yet: the empty note', () => {
  assert.match(render({round: null, model: null}), /Waiting for round 1\./);
});

test('a paused run ends the round with what resuming starts', () => {
  const html = render({endline: 'Round 2 starts when you resume.'});
  assert.match(html, /<p class="endline">Round 2 starts when you resume\.<\/p><\/div><\/div>$/);
});
