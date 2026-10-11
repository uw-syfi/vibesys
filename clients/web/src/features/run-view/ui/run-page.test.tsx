import {expect, test} from 'bun:test';
import {event} from '@vibesys/backend-client/testing';
import {initialCoreState} from '@vibesys/core-state';
import {renderToStaticMarkup} from 'react-dom/server';
import {runStep} from '../../../entities/run.js';
import {RunPage} from './run-page.js';

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
    />,
  );

  expect(html).toContain('class="transcript-content"');
  expect(html).toContain('first line\n  indented line');
  expect(html).toContain('&lt;strong&gt;not markup&lt;/strong&gt;');
  expect(html).not.toContain('<strong>not markup</strong>');
});
