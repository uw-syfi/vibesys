import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {FLEET_STATES} from '../orchestrator/fleet.js';
import {SETTINGS_TABS} from '../settings/tabs.js';
import {
  initialNavigation,
  NAVIGATION_KINDS,
  type Navigation,
  type NavigationAction,
  type NavigationKind,
  navigationReducer,
} from './navigation.js';

/** One generator per kind of move: the type makes a kind with none a compile error. */
const generators: {
  readonly [K in NavigationKind]: fc.Arbitrary<Extract<NavigationAction, {type: K}>>;
} = {
  'open-run': fc.record({
    type: fc.constant('open-run' as const),
    host: fc.constantFrom('host-a', 'host-b'),
    runId: fc.constantFrom('run-1', 'run-2'),
  }),
  'close-run': fc.constant({type: 'close-run' as const}),
  show: fc.record({
    type: fc.constant('show' as const),
    view: fc.constantFrom(
      'timeline' as const,
      'cost' as const,
      'handoffs' as const,
      'catch-up' as const,
    ),
  }),
  'pick-agent': fc.record({
    type: fc.constant('pick-agent' as const),
    agentId: fc.constantFrom('a', 'b'),
  }),
  'close-agent': fc.constant({type: 'close-agent' as const}),
  'open-runs': fc.constant({type: 'open-runs' as const}),
  'filter-runs-by-state': fc.record({
    type: fc.constant('filter-runs-by-state' as const),
    state: fc.constantFrom(...FLEET_STATES),
  }),
  'filter-runs-by-host': fc.record({
    type: fc.constant('filter-runs-by-host' as const),
    host: fc.constantFrom('host-a', 'host-b', null),
  }),
  'open-settings': fc.constant({type: 'open-settings' as const}),
  'pick-settings-tab': fc.record({
    type: fc.constant('pick-settings-tab' as const),
    tab: fc.constantFrom(...SETTINGS_TABS),
  }),
  back: fc.constant({type: 'back' as const}),
};

const action: fc.Arbitrary<NavigationAction> = fc.oneof(
  ...NAVIGATION_KINDS.map(kind => generators[kind]),
);

/** One of each move a run screen sends. */
const RUN_MOVES: readonly NavigationAction[] = [
  {type: 'open-run', host: 'host-a', runId: 'run-1'},
  {type: 'close-run'},
  {type: 'show', view: 'cost'},
  {type: 'pick-agent', agentId: 'a'},
  {type: 'close-agent'},
];

const walk = (moves: readonly NavigationAction[]): Navigation =>
  moves.reduce(navigationReducer, initialNavigation);

const moves = fc.array(action, {maxLength: 40});

/** The two moves of the run list's own controls. */
const FILTER_MOVES: readonly NavigationAction[] = [
  {type: 'filter-runs-by-state', state: 'live'},
  {type: 'filter-runs-by-host', host: 'host-b'},
];

describe('navigation', () => {
  it('starts on the run list', () => {
    expect(initialNavigation).toMatchObject({destination: 'runs', runs: {screen: 'runs'}});
  });

  it('keeps the open run while Settings shows and returns to it', () => {
    const open = walk([{type: 'open-run', host: 'host-a', runId: 'run-1'}]);
    const away = navigationReducer(open, {type: 'open-settings'});
    expect(away.destination).toBe('settings');
    expect(navigationReducer(away, {type: 'open-runs'})).toEqual(open);
    expect(navigationReducer(away, {type: 'back'})).toEqual(open);
  });

  it('the rail Runs, pressed on the runs, goes to the list from anywhere in them', () => {
    fc.assert(
      fc.property(moves, steps => {
        const nav = navigationReducer(walk([...steps, {type: 'open-runs'}]), {type: 'open-runs'});
        expect(nav.destination).toBe('runs');
        expect(nav.runs).toEqual({screen: 'runs'});
      }),
    );
  });

  it('back never leaves the app: from the list it changes nothing, from anywhere else it moves up', () => {
    fc.assert(
      fc.property(moves, steps => {
        const before = walk(steps);
        const after = navigationReducer(before, {type: 'back'});
        const atRoot = before.destination === 'runs' && before.runs.screen === 'runs';
        if (atRoot) expect(after).toEqual(before);
        else expect(after).not.toEqual(before);
      }),
    );
  });

  it('back from an agent keeps the run, and back from the run lists the runs', () => {
    const agent = walk([
      {type: 'open-run', host: 'host-a', runId: 'run-1'},
      {type: 'pick-agent', agentId: 'a'},
    ]);
    const run = navigationReducer(agent, {type: 'back'});
    expect(run.runs).toMatchObject({screen: 'run', runId: 'run-1', agentId: null});
    expect(navigationReducer(run, {type: 'back'}).runs).toEqual({screen: 'runs'});
  });

  it('the moves of a run screen mean nothing while Settings shows', () => {
    fc.assert(
      fc.property(moves, steps => {
        const settings = navigationReducer(walk(steps), {type: 'open-settings'});
        for (const move of RUN_MOVES) expect(navigationReducer(settings, move)).toBe(settings);
      }),
    );
  });

  it('a settings tab chosen in Settings stays chosen when the person leaves and returns', () => {
    fc.assert(
      fc.property(moves, fc.constantFrom(...SETTINGS_TABS), (steps, tab) => {
        const there = navigationReducer(walk(steps), {type: 'open-settings'});
        const chosen = navigationReducer(there, {type: 'pick-settings-tab', tab});
        const back = navigationReducer(navigationReducer(chosen, {type: 'back'}), {
          type: 'open-settings',
        });
        expect(back.settingsTab).toBe(tab);
      }),
    );
  });

  it('starts with every run shown and no row selected', () => {
    expect(initialNavigation.fleet).toEqual({state: 'all', host: null, selected: null});
  });

  it('keeps the list filters through a run, the settings and the rail, and finds the opened row again', () => {
    fc.assert(
      fc.property(
        fc.constantFrom(...FLEET_STATES),
        fc.constantFrom('host-a', 'host-b', null),
        (state, host) => {
          const filtered = walk([
            {type: 'filter-runs-by-state', state},
            {type: 'filter-runs-by-host', host},
          ]);
          const there = navigationReducer(filtered, {
            type: 'open-run',
            host: 'host-b',
            runId: 'run-2',
          });
          expect(there.runs.screen).toBe('run');
          for (const home of [
            navigationReducer(there, {type: 'back'}),
            navigationReducer(there, {type: 'close-run'}),
            navigationReducer(there, {type: 'open-runs'}),
            navigationReducer(
              navigationReducer(navigationReducer(there, {type: 'open-settings'}), {type: 'back'}),
              {type: 'back'},
            ),
          ]) {
            expect(home.runs).toEqual({screen: 'runs'});
            expect(home.fleet).toEqual({state, host, selected: 'host-b/run-2'});
          }
        },
      ),
    );
  });

  it('changes a filter only while the list shows, and a filter changes nothing else', () => {
    fc.assert(
      fc.property(moves, steps => {
        const before = walk(steps);
        for (const move of FILTER_MOVES) {
          const after = navigationReducer(before, move);
          if (before.destination === 'runs' && before.runs.screen === 'runs') {
            expect({...after, fleet: before.fleet}).toEqual(before);
          } else {
            expect(after).toBe(before);
          }
        }
      }),
    );
  });

  it('remembers the last run opened as the selected row, and a filter leaves it alone', () => {
    fc.assert(
      fc.property(moves, steps => {
        const nav = walk(steps);
        // `open-run` while the settings show means nothing, so only the ones that took count.
        let expected: string | null = null;
        let at = initialNavigation;
        for (const step of steps) {
          const next = navigationReducer(at, step);
          if (step.type === 'open-run' && next !== at) expected = `${step.host}/${step.runId}`;
          at = next;
        }
        expect(nav.fleet.selected).toBe(expected);
      }),
    );
  });
});
