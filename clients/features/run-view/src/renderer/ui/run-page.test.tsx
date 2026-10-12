import {expect, test} from 'bun:test';
import {event} from '@vibesys/contracts/testing';
import {runStep} from '@vibesys/run-session';
import {initialCoreState} from '@vibesys/run-state';
import {renderToStaticMarkup} from 'react-dom/server';
import {RunPage} from '../index.js';

test('surfaces a server-truncated event beside its retained payload', () => {
  const run = runStep(initialCoreState(), {
    type: 'events',
    events: [
      {
        ...event(1, 'agent_output_chunk', 'retained prefix'),
        truncated: true,
      },
    ],
  }).state;

  const html = renderToStaticMarkup(
    <RunPage
      run={run}
      session={null}
      replayFailure={null}
      onReattach={() => undefined}
      onReconnectControls={() => undefined}
      onRetryReplay={() => undefined}
      onCommand={null}
    />,
  );

  expect(html).toContain('retained prefix');
  expect(html).toContain('class="event-truncated"');
  expect(html).toContain('Event payload truncated at the server');
});

test('renders formatted transcript payloads as plain text', () => {
  const content = `first line
  indented line
${'A'.repeat(256)}
<strong>not markup</strong>`;
  const run = runStep(initialCoreState(), {
    type: 'events',
    events: [event(1, 'agent_output_chunk', content)],
  }).state;

  const html = renderToStaticMarkup(
    <RunPage
      run={run}
      session={null}
      replayFailure={null}
      onReattach={() => undefined}
      onReconnectControls={() => undefined}
      onRetryReplay={() => undefined}
      onCommand={null}
    />,
  );

  expect(html).toContain('class="transcript-content"');
  expect(html).toContain('first line\n  indented line');
  expect(html).toContain('&lt;strong&gt;not markup&lt;/strong&gt;');
  expect(html).not.toContain('<strong>not markup</strong>');
});

test('windows a large folded transcript and exposes replay controls as read-only', () => {
  const events = Array.from({length: 80}, (_, index) =>
    event(index + 1, 'agent_output_chunk', `activity ${index + 1}`),
  );
  const run = runStep(initialCoreState(), {type: 'events', events}).state;

  const html = renderToStaticMarkup(
    <RunPage
      run={run}
      session={null}
      replayFailure={null}
      onReattach={() => undefined}
      onReconnectControls={() => undefined}
      onRetryReplay={() => undefined}
      onCommand={null}
    />,
  );

  expect(html).toContain('aria-label="Transcript, 80 folded events"');
  expect(html.match(/class="transcript-row /g)?.length).toBeLessThanOrEqual(20);
  expect(html).not.toContain('activity 1</pre>');
  expect(html).toContain('activity 80</pre>');
  expect(html).toContain('tabindex="0"');
  expect(html).toContain('aria-posinset="80"');
  expect(html).toContain('aria-setsize="80"');
  expect(html).toContain('Read-only replay');
  expect(html).toContain('Controls are unavailable while replaying a recorded run.');
  expect(html).toContain('disabled=""');
});

test('renders typed tool data as a card instead of reparsing its display text', () => {
  const run = runStep(initialCoreState(), {
    type: 'events',
    events: [
      event(1, 'agent_output_chunk', {
        data: {kind: 'tool_call', tool: 'read_file', args: {path: 'src/main.py'}},
      }),
    ],
  }).state;

  const html = renderToStaticMarkup(
    <RunPage
      run={run}
      session={null}
      replayFailure={null}
      onReattach={() => undefined}
      onReconnectControls={() => undefined}
      onRetryReplay={() => undefined}
      onCommand={null}
    />,
  );

  expect(html).toContain('read_file');
  expect(html).toContain('class="tool-arguments"');
  expect(html).toContain('src/main.py');
});
