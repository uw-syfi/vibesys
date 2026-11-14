import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {FLEET_STATES} from '../orchestrator/fleet.js';
import {
  initialNavigation,
  NAVIGATION_KINDS,
  type Navigation,
  type NavigationAction,
  type NavigationKind,
  navigationReducer,
  runIsUnread,
} from './navigation.js';

/** One generator per kind of move: the type makes a kind with none a compile error. */
const generators: {
  readonly [K in NavigationKind]: fc.Arbitrary<Extract<NavigationAction, {type: K}>>;
} = {
  'open-new-run': fc.constant({type: 'open-new-run' as const}),
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
      'agents' as const,
      'cost' as const,
      'catch-up' as const,
    ),
  }),
  'show-ideas': fc.record({
    type: fc.constant('show-ideas' as const),
    window: fc.record({plans: fc.array(fc.nat(100))}),
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
  'filter-runs-by-query': fc.record({
    type: fc.constant('filter-runs-by-query' as const),
    query: fc.string({maxLength: 40}),
  }),
  'mark-run-read': fc.record({
    type: fc.constant('mark-run-read' as const),
    host: fc.constantFrom('host-a', 'host-b'),
    runId: fc.constantFrom('run-1', 'run-2'),
    activityKey: fc.string({minLength: 1, maxLength: 20}),
  }),
  'mark-run-unread': fc.record({
    type: fc.constant('mark-run-unread' as const),
    host: fc.constantFrom('host-a', 'host-b'),
    runId: fc.constantFrom('run-1', 'run-2'),
  }),
  'open-cost': fc.constant({type: 'open-cost' as const}),
  'open-machines': fc.constant({type: 'open-machines' as const}),
  'open-add-machine': fc.record({
    type: fc.constant('open-add-machine' as const),
    origin: fc.constantFrom('machines' as const, 'new-run' as const),
  }),
  'close-add-machine': fc.constant({type: 'close-add-machine' as const}),
  'open-settings': fc.constant({type: 'open-settings' as const}),
  back: fc.constant({type: 'back' as const}),
};

const action: fc.Arbitrary<NavigationAction> = fc.oneof(
  ...NAVIGATION_KINDS.map(kind => generators[kind]),
);

/** One of each move a run screen sends. */
const RUN_MOVES: readonly NavigationAction[] = [
  {type: 'close-run'},
  {type: 'show', view: 'cost'},
  {type: 'pick-agent', agentId: 'a'},
  {type: 'close-agent'},
];

const walk = (moves: readonly NavigationAction[]): Navigation =>
  moves.reduce(navigationReducer, initialNavigation);

const moves = fc.array(action, {maxLength: 40});

/** The moves of the run list's own filters. */
const FILTER_MOVES: readonly NavigationAction[] = [
  {type: 'filter-runs-by-state', state: 'live'},
  {type: 'filter-runs-by-host', host: 'host-b'},
  {type: 'filter-runs-by-query', query: 'cache'},
];

/** The rail's destinations other than the runs, each with the intent that opens it. */
const OTHERS = [
  {destination: 'cost', open: {type: 'open-cost'}},
  {destination: 'machines', open: {type: 'open-machines'}},
  {destination: 'settings', open: {type: 'open-settings'}},
] as const;

describe('navigation', () => {
  it('starts on the run list', () => {
    expect(initialNavigation).toMatchObject({destination: 'runs', runs: {screen: 'runs'}});
  });

  it.each(OTHERS)('keeps the open run while $destination shows and returns to it', other => {
    const open = walk([{type: 'open-run', host: 'host-a', runId: 'run-1'}]);
    const away = navigationReducer(open, other.open);
    expect(away.destination).toBe(other.destination);
    expect(navigationReducer(away, {type: 'open-runs'})).toEqual(open);
    expect(navigationReducer(away, {type: 'back'})).toEqual(open);
  });

  it('moves between the other destinations directly, and back leaves each for the runs', () => {
    fc.assert(
      fc.property(moves, fc.constantFrom(...OTHERS), fc.constantFrom(...OTHERS), (steps, a, b) => {
        const first = navigationReducer(walk(steps), a.open);
        const second = navigationReducer(first, b.open);
        expect(second.destination).toBe(b.destination);
        expect(second.runs).toEqual(first.runs);
        expect(second.fleet).toEqual(first.fleet);
        expect(navigationReducer(second, {type: 'back'}).destination).toBe('runs');
      }),
    );
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
        const atRoot =
          before.destination === 'runs' &&
          before.runs.screen === 'runs' &&
          before.machines.screen === 'list';
        if (atRoot) expect(after).toEqual(before);
        else expect(after).not.toEqual(before);
      }),
    );
  });

  it('back from an agent lands on the agents, then the report, then the runs', () => {
    const agent = walk([
      {type: 'open-run', host: 'host-a', runId: 'run-1'},
      {type: 'pick-agent', agentId: 'a'},
    ]);
    const agents = navigationReducer(agent, {type: 'back'});
    expect(agents.runs).toMatchObject({
      screen: 'run',
      runId: 'run-1',
      view: 'agents',
      agentId: null,
    });
    const run = navigationReducer(agents, {type: 'back'});
    expect(run.runs).toMatchObject({screen: 'run', runId: 'run-1', view: 'report', agentId: null});
    expect(navigationReducer(run, {type: 'back'}).runs).toEqual({screen: 'runs'});
  });

  it('the New run page opens in the runs destination, and back from it is the list', () => {
    const page = navigationReducer(navigationReducer(initialNavigation, {type: 'open-settings'}), {
      type: 'open-new-run',
    });
    expect(page).toMatchObject({destination: 'runs', runs: {screen: 'new-run'}});
    expect(navigationReducer(page, {type: 'back'}).runs).toEqual({screen: 'runs'});
    expect(navigationReducer(page, {type: 'open-runs'}).runs).toEqual({screen: 'runs'});
  });

  it('Add machine returns to the New run flow or the Machines list that opened it', () => {
    const newRun = walk([{type: 'open-new-run'}]);
    const fromNewRun = navigationReducer(newRun, {
      type: 'open-add-machine',
      origin: 'new-run',
    });
    expect(fromNewRun).toMatchObject({
      destination: 'runs',
      machines: {screen: 'add', origin: 'new-run'},
      runs: {screen: 'new-run'},
    });
    expect(navigationReducer(fromNewRun, {type: 'back'})).toMatchObject({
      destination: 'runs',
      machines: {screen: 'list'},
      runs: {screen: 'new-run'},
    });

    const machines = navigationReducer(initialNavigation, {type: 'open-machines'});
    const fromMachines = navigationReducer(machines, {
      type: 'open-add-machine',
      origin: 'machines',
    });
    expect(navigationReducer(fromMachines, {type: 'close-add-machine'})).toMatchObject({
      destination: 'machines',
      machines: {screen: 'list'},
    });
  });

  it('back from a screen of the run goes to its report, and from the report to the runs', () => {
    fc.assert(
      fc.property(
        fc.constantFrom('agents', 'cost', 'catch-up') as fc.Arbitrary<
          'agents' | 'cost' | 'catch-up'
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

  it('the moves of a run screen mean nothing while another destination shows', () => {
    fc.assert(
      fc.property(moves, fc.constantFrom(...OTHERS), (steps, other) => {
        const there = navigationReducer(walk(steps), other.open);
        for (const move of RUN_MOVES) expect(navigationReducer(there, move)).toBe(there);
      }),
    );
  });

  it('starts with every run shown and no row selected', () => {
    expect(initialNavigation.fleet).toEqual({
      state: 'all',
      host: null,
      query: '',
      selected: null,
      receipts: {},
    });
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
            expect(home.fleet).toEqual({
              state,
              host,
              query: '',
              selected: 'host-b/run-2',
              receipts: {},
            });
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

  it('keeps the operator query with the other list filters', () => {
    const searched = navigationReducer(initialNavigation, {
      type: 'filter-runs-by-query',
      query: '  prefix cache  ',
    });
    const run = navigationReducer(searched, {
      type: 'open-run',
      host: 'host-a',
      runId: 'run-1',
    });
    expect(navigationReducer(run, {type: 'back'}).fleet.query).toBe('  prefix cache  ');
  });

  it('records the exact revision opened, and a later revision naturally needs attention', () => {
    const opened = navigationReducer(initialNavigation, {
      type: 'open-run',
      host: 'host-a',
      runId: 'run-1',
      activityKey: 'report:4',
    });
    const receipt = opened.fleet.receipts['host-a/run-1'];
    expect(receipt).toEqual({kind: 'read', activityKey: 'report:4'});
    expect(runIsUnread(receipt, 'report:4')).toBe(false);
    expect(runIsUnread(receipt, 'report:5')).toBe(true);
  });

  it('marks a card unread or read only while the run list is showing', () => {
    const unread = navigationReducer(initialNavigation, {
      type: 'mark-run-unread',
      host: 'host-b',
      runId: 'run-2',
    });
    expect(unread.fleet.receipts['host-b/run-2']).toEqual({kind: 'unread'});
    const read = navigationReducer(unread, {
      type: 'mark-run-read',
      host: 'host-b',
      runId: 'run-2',
      activityKey: 'workstream:8',
    });
    expect(read.fleet.receipts['host-b/run-2']).toEqual({
      kind: 'read',
      activityKey: 'workstream:8',
    });
    const away = navigationReducer(read, {type: 'open-settings'});
    expect(
      navigationReducer(away, {
        type: 'mark-run-unread',
        host: 'host-b',
        runId: 'run-2',
      }),
    ).toBe(away);
  });
});
