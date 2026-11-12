import {RegistryContext} from '@effect/atom-react';
import {describe, expect, it} from '@effect/vitest';
import {RunLiveness} from '@vibesys/proto';
import * as Effect from 'effect/Effect';
import * as AsyncResult from 'effect/reactivity/AsyncResult';
import type {ReactNode} from 'react';
import {initialNavigation, type NavigationAction, type Route} from '../../state/index.js';
import {installDom, mount} from '../../testing/dom/harness.js';
import {
  FakeDaemon,
  generateScenario,
  hostHarness,
  methodPathOf,
} from '../../testing/orchestrator/index.js';
import {runEntryFrame, runsSyncedFrame, servingRuns} from '../../testing/runs-topic.js';
import {DisplayClockProvider} from '../display-clock.js';
import {TEST_ZONE} from '../test-display-clock.js';
import {nowMs} from './clock.js';
import {ConnectedApp, type Dispatch, NO_ISSUES} from './connected.js';

installDom();

const NOW = Date.UTC(2026, 10, 7, 10, 0, 0);
const noopDispatch: Dispatch = () => undefined;
const noopGo = (_action: NavigationAction): void => undefined;

const route = (runId: string): Route => ({
  screen: 'run',
  host: 'local',
  runId,
  view: 'report',
  agentId: null,
  ideas: null,
});

describe('the connected run boundary', () => {
  it.effect('opens managed history and cost streams only for runs in managed state', () =>
    Effect.gen(function* () {
      const scenario = generateScenario({seed: 73, subagents: 0, live: true});
      const h = yield* hostHarness(new FakeDaemon(scenario), {
        wrap: servingRuns(() => [
          runEntryFrame('generic-only', {
            liveness: RunLiveness.ALIVE,
            projectRoot: '/projects/generic-only',
          }),
          runsSyncedFrame,
          {hang: true},
        ]),
      });
      const releaseNavigation = h.mount(h.session.runNavigation);
      yield* h.elapse(0);
      h.registry.ensureNode(nowMs).setInitialValue(AsyncResult.success(NOW));

      const app = (open: Route): ReactNode => (
        <RegistryContext.Provider value={h.registry}>
          <DisplayClockProvider zone={TEST_ZONE}>
            <ConnectedApp
              hosts={[{key: 'local', label: 'This machine', session: h.session}]}
              dispatch={noopDispatch}
              route={open}
              fleet={initialNavigation.fleet}
              go={noopGo}
              issues={NO_ISSUES}
              setIssues={() => undefined}
            />
          </DisplayClockProvider>
        </RegistryContext.Provider>
      );
      const screen = mount(app(route('generic-only')));
      yield* h.elapse(0);
      const calls = (method: string) =>
        h.fake.calls().filter(call => call.target.method === methodPathOf(method));
      expect(calls('GetRunHistory')).toHaveLength(0);
      expect(calls('WatchRunCost')).toHaveLength(0);

      screen.render(app(route(scenario.runId)));
      yield* h.elapse(0);
      expect(calls('GetRunHistory')).not.toHaveLength(0);
      expect(calls('WatchRunCost')).not.toHaveLength(0);

      screen.unmount();
      releaseNavigation();
      yield* h.elapse(0);
    }),
  );
});
