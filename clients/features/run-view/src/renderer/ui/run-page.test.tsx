import {expect, test} from 'bun:test';
import {event, timestamp} from '@vibesys/contracts/testing';
import {runStep} from '@vibesys/run-session';
import {
  initialCoreState,
  initialIterationNavigation,
  reduceIterationResult,
} from '@vibesys/run-state';
import {renderToStaticMarkup} from 'react-dom/server';
import {RunPage} from '../index.js';

/** Any instant; tests that are not about startup timing ignore it. */
const NOW = Date.parse('2026-09-12T10:20:00Z');

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
      onCommand={null}
      now={NOW}
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
      onCommand={null}
      now={NOW}
    />,
  );

  expect(html).toContain('class="transcript-content"');
  expect(html).toContain('first line\n  indented line');
  expect(html).toContain('&lt;strong&gt;not markup&lt;/strong&gt;');
  expect(html).not.toContain('<strong>not markup</strong>');
});

test('names the run result once winner_selected reports it, not before', () => {
  const pending = renderToStaticMarkup(
    <RunPage
      run={initialCoreState()}
      iterations={initialIterationNavigation()}
      session={null}
      replayFailure={null}
      onReattach={() => undefined}
      onReconnectControls={() => undefined}
      onRetryReplay={() => undefined}
      onCommand={null}
      now={NOW}
    />,
  );
  expect(pending).toContain('<span>Result</span><strong>Pending</strong>');

  const run = runStep(initialCoreState(), {
    type: 'events',
    events: [
      event(1, 'winner_selected', {
        data: {
          kind: 'winner_selected',
          selection: 'candidate',
          revision: 'abc1234def',
          settlement_id: 'settlement-7',
        },
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
      onCommand={null}
      now={NOW}
    />,
  );
  expect(html).toContain('<span>Result</span><strong>Candidate abc1234 · settlement-7</strong>');
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
      onCommand={null}
      now={NOW}
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

  const iterations = reduceIterationResult(initialIterationNavigation(), {
    kind: 'page',
    schema_version: 1,
    run_id: 'run-1',
    projection_revision: 'empty-policy',
    parent_id: null,
    from_cursor: null,
    items: [],
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
      onCommand={null}
      now={NOW}
    />,
  );

  expect(html).toContain('<span>Iterations</span><strong>0</strong>');
  expect(html).not.toContain('0 / 3');
});

test('windows a large folded transcript and exposes replay controls as read-only', () => {
  const events = Array.from({length: 80}, (_, index) =>
    event(index + 1, 'agent_output_chunk', `activity ${index + 1}`),
  );
  const run = runStep(initialCoreState(), {type: 'events', events}).state;

  const html = renderToStaticMarkup(
    <RunPage
      run={run}
      iterations={initialIterationNavigation()}
      session={null}
      replayFailure={null}
      onReattach={() => undefined}
      onReconnectControls={() => undefined}
      onRetryReplay={() => undefined}
      onCommand={null}
      now={NOW}
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
      iterations={initialIterationNavigation()}
      session={null}
      replayFailure={null}
      onReattach={() => undefined}
      onReconnectControls={() => undefined}
      onRetryReplay={() => undefined}
      onCommand={null}
      now={NOW}
    />,
  );

  expect(html).toContain('read_file');
  expect(html).toContain('class="tool-arguments"');
  expect(html).toContain('src/main.py');
});

test('narrates a starting run environment progress in the status chip', () => {
  const run = runStep(
    {...initialCoreState(), status: 'starting'},
    {
      type: 'events',
      events: [
        event(1, 'run_startup_progress', {
          data: {
            kind: 'run_startup_progress',
            stage: 'building_image',
            progress: {done: 12, total: 30, label: 'RUN pip install'},
          },
        }),
      ],
    },
  ).state;

  const html = renderToStaticMarkup(
    <RunPage
      run={run}
      iterations={initialIterationNavigation()}
      session={null}
      replayFailure={null}
      onReattach={() => undefined}
      onReconnectControls={() => undefined}
      onRetryReplay={() => undefined}
      onCommand={null}
      now={Date.parse(timestamp(1)) + 3_000}
    />,
  );

  expect(html).toContain('class="status status-starting"');
  expect(html).toContain('Building image 12/30: RUN pip install · 3s');
});

test('shows how long the starting stage has run, and that it has gone quiet', () => {
  const run = runStep(
    {...initialCoreState(), status: 'starting'},
    {
      type: 'events',
      events: [
        event(1, 'run_startup_progress', {
          data: {
            kind: 'run_startup_progress',
            stage: 'building_image',
            progress: {done: 7, total: 8, label: 'WORKDIR'},
          },
        }),
      ],
    },
  ).state;
  const at = Date.parse(timestamp(1));
  const render = (now: number): string =>
    renderToStaticMarkup(
      <RunPage
        run={run}
        iterations={initialIterationNavigation()}
        session={null}
        replayFailure={null}
        onReattach={() => undefined}
        onReconnectControls={() => undefined}
        onRetryReplay={() => undefined}
        onCommand={null}
        now={now}
      />,
    );

  expect(render(at + 252_000)).toContain('Building image 7/8: WORKDIR · 4m12s · quiet for 4m12s');
  expect(render(at + 5_000)).toContain('Building image 7/8: WORKDIR · 5s</span>');
});
