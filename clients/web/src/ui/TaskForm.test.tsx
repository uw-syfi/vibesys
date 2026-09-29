import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {renderToStaticMarkup} from 'react-dom/server';
import type {Draft} from '../setup.js';
import {TaskFormRows} from './TaskForm.js';

const EMPTY: Draft = {
  name: '',
  objective: '',
  domain: 'generic',
  accuracy_command: '',
  benchmark_command: '',
  result_json_argument: '--json',
  result_metric: '',
};
const FULL: Draft = {
  name: 'decode',
  objective: 'Faster decode.',
  domain: 'llm-serving',
  accuracy_command: 'cargo test --release',
  benchmark_command: 'cargo bench --bench decode',
  result_json_argument: '--json',
  result_metric: 'median_tok_per_sec',
};
const none = () => undefined;
const render = (draft: Draft, creating: boolean, error: string | null = null, conflict = false) =>
  renderToStaticMarkup(
    <TaskFormRows
      draft={draft}
      creating={creating}
      saving={false}
      error={error}
      conflict={conflict}
      onDraft={none}
      onSave={none}
      onDiscard={none}
    />,
  );

test('a new task asks for a name; an edited one does not', () => {
  assert.match(render(EMPTY, true), /<label for="f-name">Name<\/label>/);
  assert.doesNotMatch(render(FULL, false), /f-name/);
  const html = render(FULL, false);
  for (const id of ['f-objective', 'f-domain', 'f-accuracy', 'f-benchmark', 'f-metric']) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(html, /<option value="llm-serving" selected="">LLM serving<\/option>/);
  assert.match(html, /aria-label="JSON flag"/);
  assert.match(
    html,
    /Read from the benchmark&#x27;s <code class="mono">--json<\/code> output\.\s+Higher is better\./,
  );
});

test('a name outside the task name rule says so under the field', () => {
  assert.doesNotMatch(render(EMPTY, true), /letter or digit/);
  assert.match(
    render({...EMPTY, name: 'Decode'}, true),
    /<div id="f-name-hint" class="hint bad">Up to 128 of a-z, 0-9, \., _ or -, starting with a letter or digit\.<\/div>/,
  );
});

test('Save is enabled only for a complete form; a save error shows beside it', () => {
  assert.match(
    render(EMPTY, true),
    /<button id="f-save" type="button" class="btn" disabled="">Save task<\/button>/,
  );
  assert.match(
    render(FULL, true),
    /<button id="f-save" type="button" class="btn">Save task<\/button>/,
  );
  assert.match(
    render(FULL, false, 'The task changed on disk'),
    /<span class="hint bad" role="alert">The task changed on disk<\/span>/,
  );
  // After a conflict, Save stays off until Discard reloads the task.
  assert.match(
    render(FULL, false, 'The task changed on disk', true),
    /<button id="f-save" type="button" class="btn" disabled="" title="Discard to load the task from disk, then edit again">Save task<\/button>/,
  );
});
