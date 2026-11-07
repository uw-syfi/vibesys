import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {BuildVersionsSchema} from '@vibesys/orchestrator-proto';
import {type JSX, useReducer} from 'react';
import {
  applyChange,
  type CostFeedSnapshot,
  costTarget,
  emptyState,
  initialRoute,
  type RouteAction,
  routeReducer,
} from '../../state/index.js';
import {installDom, mount} from '../../testing/dom/harness.js';
import {PRICED_AND_UNPRICED} from '../../testing/orchestrator/cost-views.js';
import {generateScenario} from '../../testing/orchestrator/index.js';
import {type HostSnapshot, OrchestratorApp} from '../orchestrator/app.js';

installDom();

const scenario = generateScenario({seed: 3, subagents: 2, live: true});
const base = scenario.changes.reduce(applyChange, emptyState);
const COSTS = {[scenario.runId]: {...PRICED_AND_UNPRICED, runId: scenario.runId}};
const live: CostFeedSnapshot = {status: {_tag: 'Live'}, freshness: 'live'};

function hostWith(
  costs: typeof COSTS | Record<string, never>,
  feed: CostFeedSnapshot | undefined,
): HostSnapshot {
  return {
    key: 'local',
    label: 'This machine',
    model: {data: {...base, costs}, agentCursors: {}},
    connection: {_tag: 'Ready', epoch: 1, daemon: create(BuildVersionsSchema)},
    freshness: {status: 'live', lastUpdateMs: null},
    runsFeed: {_tag: 'Live'},
    agentFeeds: {},
    history: {earlier: {_tag: 'Idle'}, wholes: {}},
    costFeeds: feed === undefined ? {} : {[scenario.runId]: feed},
  };
}

/** The runs as the shell composes them, over the route fold, recording what is watched and retried. */
function Window(props: {
  readonly host: HostSnapshot;
  readonly log: string[];
  readonly retries: string[];
}): JSX.Element {
  const [route, move] = useReducer(routeReducer, initialRoute);
  const go = (action: RouteAction): void => {
    move(action);
  };
  // What the entry would mount: the log says which run's cost stream is open after each render.
  props.log.push(JSON.stringify(costTarget(route)));
  return (
    <OrchestratorApp
      hosts={[props.host]}
      nowMs={scenario.endMs}
      commands={{
        start: undefined,
        stop: {},
        send: {},
        startRun: () => undefined,
        retryStart: () => undefined,
        stopRun: () => undefined,
        retryStop: () => undefined,
        sendMessage: () => undefined,
        retrySend: () => undefined,
        loadEarlier: () => undefined,
        expand: () => undefined,
      }}
      retryHost={() => undefined}
      retryCost={(host, runId) => props.retries.push(`${host}/${runId}`)}
      route={route}
      go={go}
    />
  );
}

function openCost(host: HostSnapshot) {
  const log: string[] = [];
  const retries: string[] = [];
  const screen = mount(<Window host={host} log={log} retries={retries} />);
  const [row] = screen.container.querySelectorAll<HTMLElement>('tr[data-selectable]');
  if (row === undefined) throw new Error('no run listed');
  screen.press(row);
  return {screen, log, retries};
}

describe('the Cost view under a run', () => {
  it('is a tab of the run that shows the served figures, and leaving it shows none', () => {
    const {screen} = openCost(hostWith(COSTS, live));
    // The run opens on its report, which shows the spend in brief and links to the Cost view.
    expect(screen.text()).toContain('≥\u2005$318');
    expect(screen.container.querySelector('[role="tablist"]')).toBeNull();
    screen.press(screen.control('Open cost'));
    expect(screen.container.querySelector('[role="tablist"]')).not.toBeNull();
    expect(screen.text()).toContain('≥\u2005$318');
    expect(screen.text()).toContain('of $500');
    expect(screen.text()).toContain('By agent type');
    screen.press(screen.control('Timeline'));
    expect(screen.text()).not.toContain('By agent type');
    screen.unmount();
  });

  it('watches the cost stream only while the report or the Cost tab is showing', () => {
    const {screen, log} = openCost(hostWith(COSTS, live));
    const watching = () => JSON.parse(log.at(-1) ?? 'null');
    expect(watching()).toEqual({host: 'local', runId: scenario.runId});
    screen.press(screen.control('Open agents'));
    expect(watching()).toBeNull();
    screen.press(screen.control('Cost'));
    expect(watching()).toEqual({host: 'local', runId: scenario.runId});
    screen.press(screen.control('Handoffs and reports'));
    expect(watching()).toBeNull();
    screen.press(screen.control('Cost'));
    expect(watching()).toEqual({host: 'local', runId: scenario.runId});
    screen.unmount();
  });

  it('shows loading, with no figures and no banner, until the host has sent a view', () => {
    const {screen} = openCost(hostWith({}, {status: {_tag: 'Opening'}, freshness: 'empty'}));
    screen.press(screen.control('Open cost'));
    expect(screen.text()).toContain('Loading');
    expect(screen.text()).not.toContain('By agent type');
    screen.unmount();
  });

  it('shows an old host as unavailable while the rest of the run still works', () => {
    const {screen} = openCost(
      hostWith(
        {},
        {
          status: {
            _tag: 'Failed',
            fault: {kind: 'Unimplemented', detail: 'no such method', retryable: false},
          },
          freshness: 'unavailable',
        },
      ),
    );
    screen.press(screen.control('Open cost'));
    expect(screen.text()).toContain('Unavailable');
    expect(screen.text()).toContain('too old');
    expect(screen.container.querySelectorAll('button').length).toBeGreaterThan(0);
    // The other views of the run are still there to press.
    screen.press(screen.control('Timeline'));
    expect(screen.text()).not.toContain('Unavailable');
    screen.unmount();
  });

  it('retries the failed stream of this run on this host when Retry is pressed', () => {
    const {screen, retries} = openCost(
      hostWith(COSTS, {
        status: {
          _tag: 'Failed',
          fault: {kind: 'DeadlineExceeded', detail: 'deadline', retryable: true},
        },
        freshness: 'stale',
      }),
    );
    screen.press(screen.control('Open cost'));
    // The last view stays on screen, marked stale, with the way to try again beside it.
    expect(screen.text()).toContain('Stale');
    expect(screen.text()).toContain('≥\u2005$318');
    screen.press(screen.control('Retry cost'));
    expect(retries).toEqual([`local/${scenario.runId}`]);
    screen.unmount();
  });
});
