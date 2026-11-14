import {describe, expect, it} from '@effect/vitest';
import {type JSX, useReducer} from 'react';
import {
  applyChange,
  type CostFeedSnapshot,
  costTarget,
  emptyState,
  initialNavigation,
  type NavigationAction,
  navigationReducer,
} from '../../state/index.js';
import {appCommandsOf, hostSnapshotOf} from '../../testing/dom/app-builders.js';
import {installDom, mount} from '../../testing/dom/harness.js';
import {PRICED_AND_UNPRICED} from '../../testing/orchestrator/cost-views.js';
import {generateScenario} from '../../testing/orchestrator/index.js';
import {type HostSnapshot, OrchestratorApp} from '../orchestrator/app.js';
import {runTitle} from '../orchestrator/fleet-words.js';

installDom();

const scenario = generateScenario({seed: 3, subagents: 2, live: true});
const base = scenario.changes.reduce(applyChange, emptyState);
const COSTS = {[scenario.runId]: {...PRICED_AND_UNPRICED, runId: scenario.runId}};
const live: CostFeedSnapshot = {status: {_tag: 'Live'}, freshness: 'live'};

function hostWith(
  costs: typeof COSTS | Record<string, never>,
  feed: CostFeedSnapshot | undefined,
): HostSnapshot {
  return hostSnapshotOf({
    model: {data: {...base, costs}, agentCursors: {}},
    costFeeds: feed === undefined ? {} : {[scenario.runId]: feed},
  });
}

/** The runs as the shell composes them, over the navigation fold, recording what is watched and retried. */
function Window(props: {
  readonly host: HostSnapshot;
  readonly log: string[];
  readonly retries: string[];
}): JSX.Element {
  const [navigation, move] = useReducer(navigationReducer, initialNavigation);
  const go = (action: NavigationAction): void => {
    move(action);
  };
  // What the entry would mount: the log says which run's cost stream is open after each render.
  props.log.push(JSON.stringify(costTarget(navigation.runs)));
  return (
    <OrchestratorApp
      hosts={[props.host]}
      nowMs={scenario.endMs}
      commands={appCommandsOf()}
      retryHost={() => undefined}
      retryCost={(host, runId) => props.retries.push(`${host}/${runId}`)}
      route={navigation.runs}
      fleet={navigation.fleet}
      go={go}
    />
  );
}

function openCost(host: HostSnapshot) {
  const log: string[] = [];
  const retries: string[] = [];
  const screen = mount(<Window host={host} log={log} retries={retries} />);
  const [row] = screen.container.querySelectorAll<HTMLElement>('.vs-summary-card-press');
  if (row === undefined) throw new Error('no run listed');
  screen.press(row);
  return {screen, log, retries};
}

/** What the run is called on the list and in every trail. */
const run = base.runs[scenario.runId];
const TASK = run === undefined ? '' : runTitle(run.taskCard ?? null, run.task);

const title = (screen: ReturnType<typeof mount>): string | undefined =>
  screen.container.querySelector('h1')?.textContent ?? undefined;

describe('the Cost destination of a run', () => {
  it('is a page of its own that shows the served figures, with the report one step back', () => {
    const {screen} = openCost(hostWith(COSTS, live));
    // The run opens on its report, which shows the spend in brief and links to the Cost page.
    expect(screen.text()).toContain('$318 spent');
    expect(title(screen)).not.toBe('Cost');
    screen.press(screen.control('Open cost'));
    expect(title(screen)).toBe('Cost');
    expect(screen.container.querySelector('[role="tablist"]')).toBeNull();
    expect(screen.text()).toContain('≥ $318');
    expect(screen.text()).toContain('of $500');
    expect(screen.text()).toContain('By agent type');
    // The trail names the way back: Runs, the run, and this page as the last step.
    screen.press(screen.control(TASK));
    expect(title(screen)).toBe(TASK);
    screen.press(screen.control('Open cost'));
    screen.press(screen.control('Runs'));
    expect(title(screen)).toBe('Runs');
    screen.unmount();
  });

  it('watches the cost stream only while the report or the Cost page is showing', () => {
    const {screen, log} = openCost(hostWith(COSTS, live));
    const watching = () => JSON.parse(log.at(-1) ?? 'null');
    expect(watching()).toEqual({host: 'local', runId: scenario.runId});
    screen.press(screen.control('Open agents'));
    expect(watching()).toBeNull();
    screen.press(screen.control(TASK));
    expect(watching()).toEqual({host: 'local', runId: scenario.runId});
    screen.press(screen.control('Open cost'));
    expect(watching()).toEqual({host: 'local', runId: scenario.runId});
    screen.press(screen.control(TASK));
    screen.press(screen.control('Open agents'));
    expect(watching()).toBeNull();
    screen.unmount();
  });

  it('shows loading, with no figures and no banner, until the host has sent a view', () => {
    const {screen} = openCost(hostWith({}, {status: {_tag: 'Opening'}, freshness: 'empty'}));
    screen.press(screen.control('Open cost'));
    expect(screen.text()).toContain('Waiting');
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
    expect(screen.text()).toContain('≥ $318');
    screen.press(screen.control('Retry cost'));
    expect(retries).toEqual([`local/${scenario.runId}`]);
    screen.unmount();
  });
});
