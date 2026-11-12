import {describe, expect, it} from '@effect/vitest';
import {useReducer} from 'react';
import {
  type GenericRunEntry,
  type GenericRunIndex,
  initialNavigation,
  type NavigationAction,
  navigationReducer,
} from '../../state/index.js';
import {appCommandsOf, hostSnapshotOf} from '../../testing/dom/app-builders.js';
import {installDom, mount} from '../../testing/dom/harness.js';
import {DisplayClockProvider} from '../display-clock.js';
import {TEST_ZONE} from '../test-display-clock.js';
import {OrchestratorApp} from './app.js';

installDom();

const NOW = Date.UTC(2026, 10, 7, 10, 0, 0);

const genericRun = (overrides: Partial<GenericRunEntry> = {}): GenericRunEntry => ({
  runId: 'generic-run',
  projectRoot: '/projects/serve',
  state: 'running',
  activity: 'live',
  objective: {name: 'Throughput', unit: 'requests/s', bestValue: 87.5},
  costUsd: 4.25,
  needsUser: true,
  ...overrides,
});

function Window(props: {
  readonly run: GenericRunEntry;
  readonly freshness?: 'current' | 'stale';
  readonly index?: GenericRunIndex;
  readonly open?: boolean;
}) {
  const start = props.open
    ? navigationReducer(initialNavigation, {
        type: 'open-run',
        host: 'local',
        runId: props.run.runId,
      })
    : initialNavigation;
  const [navigation, move] = useReducer(navigationReducer, start);
  const go = (action: NavigationAction): void => move(action);
  const host = hostSnapshotOf({
    runIndex: props.index ?? {
      _tag: 'Ready',
      freshness: props.freshness ?? 'current',
      runs: new Map([[props.run.runId, props.run]]),
    },
  });
  return (
    <DisplayClockProvider zone={TEST_ZONE}>
      <OrchestratorApp
        hosts={[host]}
        nowMs={NOW}
        commands={appCommandsOf()}
        retryHost={() => undefined}
        retryCost={() => undefined}
        route={navigation.runs}
        fleet={navigation.fleet}
        go={go}
      />
    </DisplayClockProvider>
  );
}

const draw = (run = genericRun(), freshness: 'current' | 'stale' = 'current') =>
  mount(<Window run={run} freshness={freshness} />);

const drawOpen = (index: GenericRunIndex) =>
  mount(<Window run={genericRun()} index={index} open />);

describe('generic run discovery', () => {
  it('shows only authoritative index facts and routes a generic-only row to a safe run page', () => {
    const screen = draw();
    const row = screen.container.querySelector<HTMLElement>('[role="option"]');
    if (row === null) throw new Error('the generic run has no row');
    expect(row.textContent).toContain('serve');
    expect(row.textContent).toContain('generic-run');
    expect(row.textContent).toContain('Running');
    expect(row.textContent).toContain('Needs you · Up to date');
    expect(row.textContent).toContain('87.5 requests/s');
    expect(row.textContent).toContain('$4.25');
    expect(row.querySelector('[data-narrow-only]')?.textContent).toBe(' · This machine');
    screen.press(row);
    expect(screen.container.querySelector('h1')?.textContent).toBe('serve');
    expect(screen.text()).toContain('Run details unavailable');
    expect(screen.text()).toContain('Input needed');
    expect(screen.text()).toContain(
      'This run is waiting for a response. Return to the client or workflow that started it to continue.',
    );
    expect(screen.text()).toContain(
      'This run is listed by the host. Detailed activity is not available in this app version.',
    );
    expect(screen.text()).toContain('Up to date');
    expect(screen.text()).toContain('Project serve');
    expect(screen.text()).not.toContain('not on this host');
    expect(screen.container.querySelector('[data-mono-id]')?.textContent).toBe('generic-run');
    screen.unmount();
  });

  it('keeps stale and unknown states explicit instead of turning either into a finished run', () => {
    const screen = draw(
      genericRun({
        state: 'unknown',
        activity: 'unknown',
        projectRoot: null,
        objective: null,
        costUsd: null,
        needsUser: false,
      }),
      'stale',
    );
    expect(screen.text()).toContain('Status unavailable');
    expect(screen.text()).toContain('Last known');
    expect(screen.text()).toContain('Not available');
    expect(screen.text()).toContain('Not listed');
    expect(screen.text()).toContain('Not recorded');
    const row = screen.container.querySelector('[role="option"]');
    expect(row?.querySelectorAll('[data-wide-only]')).toHaveLength(4);
    expect(
      [...(row?.querySelectorAll<HTMLElement>('[data-wide-only]') ?? [])].map(
        element => element.textContent,
      ),
    ).toEqual(['Not available', 'Not recorded', 'Not listed', 'Not recorded']);
    expect(
      [...(row?.querySelectorAll<HTMLElement>('[data-narrow-only]') ?? [])]
        .filter(element => element.textContent === '—')
        .map(element => element.parentElement?.title),
    ).toEqual(['Not available', 'Not recorded', 'Not listed', 'Not recorded']);
    expect(screen.text()).not.toContain('Finished');
    screen.unmount();
  });

  it('preserves held, parking and stopping intent in the row a person sees', () => {
    for (const [state, label] of [
      ['held', 'Held'],
      ['parking', 'Parking'],
      ['stopping', 'Stopping'],
    ] as const) {
      const screen = draw(genericRun({state, activity: 'live', needsUser: false}));
      const row = screen.container.querySelector<HTMLElement>('[role="option"]');
      expect(row?.textContent, state).toContain(label);
      expect(row?.getAttribute('aria-description'), state).toContain(label);
      screen.unmount();
    }
  });

  it('keeps a persisted route while the generic index is opening or unavailable', () => {
    const loading = drawOpen({_tag: 'Loading'});
    expect(loading.text()).toContain('Run index opening');
    expect(loading.text()).toContain("Waiting for the host's first complete run-index snapshot.");
    expect(loading.text()).not.toContain('not on this host');
    loading.unmount();

    const unavailable = drawOpen({_tag: 'Unavailable', reason: 'unsupported'});
    expect(unavailable.text()).toContain('Run index unavailable');
    expect(unavailable.text()).toContain('This host does not support generic run discovery.');
    expect(unavailable.text()).not.toContain('not on this host');
    unavailable.unmount();
  });

  it('calls a run missing only after a current complete index proves it absent', () => {
    const stale = drawOpen({_tag: 'Ready', freshness: 'stale', runs: new Map()});
    expect(stale.text()).toContain('Run status unconfirmed');
    expect(stale.text()).toContain(
      'The last complete run index does not include this run, but that snapshot is stale. Waiting for a current snapshot to confirm.',
    );
    expect(stale.text()).not.toContain('not on this host');
    stale.unmount();

    const current = drawOpen({_tag: 'Ready', freshness: 'current', runs: new Map()});
    expect(current.text()).toContain('This run is not on this host any more.');
    expect(current.text()).not.toContain('Run index opening');
    expect(current.text()).not.toContain('Run index unavailable');
    expect(current.text()).not.toContain('Run status unconfirmed');
    current.unmount();
  });
});
