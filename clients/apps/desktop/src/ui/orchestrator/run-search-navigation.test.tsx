import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  RunSchema,
  RunSearchResultSchema,
  RunStatus,
  RunSummarySchema,
  SearchRunsResponseSchema,
  TaskCardSchema,
} from '@vibesys/orchestrator-proto';
import {initialNavigation, navigationReducer} from '../../state/index.js';
import {appCommandsOf, hostSnapshotOf} from '../../testing/dom/app-builders.js';
import {installDom, mount} from '../../testing/dom/harness.js';
import {OrchestratorApp} from './app.js';

installDom();

describe('opening a whole-history run search result', () => {
  it('opens the authoritative run-summary pair without pretending the feed already holds it', () => {
    const run = create(RunSchema, {
      id: 'run-archived',
      task: 'Archived allocator trial',
      repository: '/work/allocator',
      status: RunStatus.ENDED,
      createdAt: timestampFromMs(1_000),
      endedAt: timestampFromMs(2_000),
      taskCard: create(TaskCardSchema, {
        task: 'Archived allocator trial',
        heading: 'Reduce allocator fragmentation',
      }),
    });
    const result = create(RunSearchResultSchema, {
      run,
      summary: create(RunSummarySchema, {runId: run.id}),
    });
    const host = hostSnapshotOf({
      runSearch: {
        _tag: 'Found',
        query: 'allocator',
        response: create(SearchRunsResponseSchema, {sequence: 8n, results: [result]}),
      },
    });
    const navigation = navigationReducer(initialNavigation, {
      type: 'open-run',
      host: host.key,
      runId: run.id,
      activityKey: 'ended:0:none:',
    });
    const screen = mount(
      <OrchestratorApp
        hosts={[host]}
        nowMs={3_000}
        commands={appCommandsOf()}
        retryHost={() => undefined}
        retryCost={() => undefined}
        route={navigation.runs}
        fleet={navigation.fleet}
        go={() => undefined}
      />,
    );

    expect(screen.container.querySelector('h1')?.textContent).toContain(
      'Reduce allocator fragmentation',
    );
    expect(screen.text()).not.toContain('This run is not on this host any more.');
    // The overlay is transient: constructing the destination did not mutate the feed model.
    expect(host.model.data.runs[run.id]).toBeUndefined();
    screen.unmount();
  });
});
