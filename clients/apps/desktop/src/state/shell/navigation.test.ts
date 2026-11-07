import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
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
      'report' as const,
      'timeline' as const,
      'agents' as const,
      'cost' as const,
      'handoffs' as const,
    ),
  }),
  'pick-agent': fc.record({
    type: fc.constant('pick-agent' as const),
    agentId: fc.constantFrom('a', 'b'),
  }),
  'close-agent': fc.constant({type: 'close-agent' as const}),
  'open-runs': fc.constant({type: 'open-runs' as const}),
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

  it('back from a screen of the run goes to its report, and from the report to the runs', () => {
    fc.assert(
      fc.property(
        fc.constantFrom('timeline', 'agents', 'cost', 'handoffs') as fc.Arbitrary<
          'timeline' | 'agents' | 'cost' | 'handoffs'
        >,
        view => {
          const screen = walk([
            {type: 'open-run', host: 'host-a', runId: 'run-1'},
            {type: 'show', view},
          ]);
          const report = navigationReducer(screen, {type: 'back'});
          expect(report.runs).toMatchObject({screen: 'run', runId: 'run-1', view: 'report'});
          expect(navigationReducer(report, {type: 'back'}).runs).toEqual({screen: 'runs'});
        },
      ),
    );
  });

  it('a run always opens on its report, and the report is reached by back from anything of the run', () => {
    fc.assert(
      fc.property(moves, steps => {
        let nav = navigationReducer(walk(steps), {type: 'open-runs'});
        nav = navigationReducer(nav, {type: 'open-run', host: 'host-a', runId: 'run-1'});
        expect(nav.runs).toMatchObject({view: 'report', agentId: null});
        nav = navigationReducer(nav, {type: 'show', view: 'cost'});
        nav = navigationReducer(nav, {type: 'pick-agent', agentId: 'a'});
        nav = navigationReducer(navigationReducer(nav, {type: 'back'}), {type: 'back'});
        expect(nav.runs).toMatchObject({view: 'report'});
      }),
    );
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
});
