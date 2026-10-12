import {expect, test} from 'bun:test';
import {event} from '@vibesys/contracts/testing';
import {runStep} from '@vibesys/run-session';
import {
  initialCoreState,
  initialIterationNavigation,
  reduceIterationResult,
} from '@vibesys/run-state';
import {renderToStaticMarkup} from 'react-dom/server';
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
      iterations={initialIterationNavigation()}
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
      iterations={initialIterationNavigation()}
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

test('does not present a round budget as a limit on non-round iterations', () => {
  const run = runStep(initialCoreState(), {
    type: 'events',
    events: [
      event(1, 'run_started', {
        run_id: 'run-1',
        data: {kind: 'run_started', outer_loop: 'dynamic', input: '.', max_rounds: 3},
      }),
    ],
  }).state;
  const iterations = reduceIterationResult(initialIterationNavigation(), {
    kind: 'page',
    schema_version: 1,
    run_id: 'run-1',
    projection_revision: 'workstreams',
    parent_id: null,
    from_cursor: null,
    items: [
      {
        id: 'workstream:cache',
        parent_id: null,
        kind: 'workstream',
        label: 'Cache lookups',
        ordinal: 1,
        status: 'active',
        child_count: 2,
        headline_metrics: [],
      },
    ],
    next_cursor: null,
  });

  const html = renderToStaticMarkup(
    <RunPage
      run={run}
      iterations={iterations}
      session={null}
      replayFailure={null}
      onReattach={() => undefined}
      onReconnectControls={() => undefined}
      onRetryReplay={() => undefined}
    />,
  );

  expect(html).toContain('<span>Iterations</span><strong>1</strong>');
  expect(html).not.toContain('1 / 3');
});

test('does not infer round semantics from an empty policy projection', () => {
  const run = runStep(initialCoreState(), {
    type: 'events',
    events: [
      event(1, 'run_started', {
        run_id: 'run-1',
        data: {kind: 'run_started', outer_loop: 'dynamic', input: '.', max_rounds: 3},
      }),
    ],
  }).state;

  const html = renderToStaticMarkup(
    <RunPage
      run={run}
      iterations={initialIterationNavigation()}
      session={null}
      replayFailure={null}
      onReattach={() => undefined}
      onReconnectControls={() => undefined}
      onRetryReplay={() => undefined}
    />,
  );

  expect(html).toContain('<span>Iterations</span><strong>0</strong>');
  expect(html).not.toContain('0 / 3');
});
