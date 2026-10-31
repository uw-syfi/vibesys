import {describe, expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  type Route,
  RUN_MODES,
  RUN_ROUTE,
  type RunMode,
  routeFromQuery,
  routeQuery,
  routeUp,
  type ZoomEvent,
  zoomStep,
} from './zoom.js';

const IDS = ['a', 'b c', 'flow:w1/#1', 'x&y=z', '#hash'];

function randomRoute(random: Seeded): Route {
  const opened = random.int(3);
  const idea = opened === 1 ? random.pick(IDS) : null;
  const agent = opened === 2 ? random.pick(IDS) : null;
  const step = opened > 0 && random.bool() ? random.pick(IDS) : null;
  const item = step !== null && random.bool() ? random.pick(IDS) : null;
  const mode = idea === null ? random.pick(RUN_MODES) : random.pick<RunMode>(['ideas', 'logs']);
  return {mode, idea, agent, step, item};
}

function randomZoom(random: Seeded): ZoomEvent {
  return random.pick<ZoomEvent>([
    {type: 'open-idea', idea: random.pick(IDS)},
    {type: 'open-agent', agent: random.pick(IDS)},
    {type: 'open-step', step: random.pick(IDS)},
    {type: 'open-step', step: random.pick(IDS), idea: random.pick(IDS)},
    {type: 'open-item', item: random.pick(IDS)},
    {type: 'mode', mode: random.pick(RUN_MODES)},
    {type: 'up'},
    {type: 'up'},
    {type: 'jump', route: randomRoute(random)},
  ]);
}

/** The window's history as the page drives it: entries of fragments, and where it stands. */
class FakeHistory {
  readonly entries: string[];
  at = 0;

  constructor(fragment: string) {
    this.entries = [fragment];
  }

  /** Adds an entry after the one it stands at, dropping any ahead, unless it names the same. */
  move(fragment: string): void {
    if (fragment === this.fragment) return;
    this.entries.splice(this.at + 1, this.entries.length, fragment);
    this.at = this.entries.length - 1;
  }

  /** Goes `by` entries back (negative) or forward, stopping at either end. */
  go(by: number): void {
    this.at = Math.max(0, Math.min(this.entries.length - 1, this.at + by));
  }

  get fragment(): string {
    return this.entries[this.at] ?? '';
  }
}

/**
 * Drives the route as the page does for `steps` random moves: a user's zoom moves the history, a
 * Back or Forward moves it without one. The route is only ever read from the entry it stands at.
 */
function walkHistory(random: Seeded, steps: number): void {
  const history = new FakeHistory(routeQuery(randomRoute(random)));
  for (let n = 0; n < steps; n += 1) {
    if (random.int(4) === 0) history.go(random.bool() ? -1 : 1);
    else {
      const route = routeFromQuery(history.fragment);
      const next = zoomStep(route, randomZoom(random));
      history.move(routeQuery(next));
      expect(routeFromQuery(history.fragment)).toEqual(next);
    }
    expect(routeProblems(routeFromQuery(history.fragment))).toEqual([]);
  }
}

/** The route after `events` from the run, and the addresses the zooms moved through. */
function zoom(...events: ZoomEvent[]): {route: Route; remembered: string[]} {
  const history = new FakeHistory('');
  for (const event of events) {
    history.move(routeQuery(zoomStep(routeFromQuery(history.fragment), event)));
  }
  return {route: routeFromQuery(history.fragment), remembered: history.entries.slice(1)};
}

/** What a route must never say: two things open, an item without its step, a hidden idea. */
function routeProblems(route: Route): string[] {
  const hidden = route.mode === 'timeline' || route.mode === 'agents';
  return [
    ...(route.idea !== null && route.agent !== null ? ['an idea and an agent open'] : []),
    ...(route.idea !== null && hidden
      ? [`an idea open under the ${route.mode} face, which would hide it`]
      : []),
    ...(route.item !== null && route.step === null ? ['an item without its step'] : []),
  ];
}

describe('the zoom', () => {
  test('after any zooms, Backs and Forwards, each route is one the address can name', () => {
    for (const seed of SEEDS) walkHistory(new Seeded(seed), 200);
  });

  test('a crumb jumps up and is remembered, so Back returns to the level it left', () => {
    const steps = [
      {type: 'open-idea', idea: 'i1'},
      {type: 'open-step', step: 's1'},
    ] as const;
    expect(zoom(...steps, {type: 'jump', route: RUN_ROUTE}).remembered).toEqual([
      'idea=i1',
      'idea=i1&step=s1',
      '',
    ]);
    const toIdea = zoom(...steps, {type: 'jump', route: {...RUN_ROUTE, idea: 'i1'}});
    expect(toIdea.route).toEqual({...RUN_ROUTE, idea: 'i1'});
    expect(toIdea.remembered.at(-1)).toBe('idea=i1');
    // A jump to the route shown adds no history entry.
    expect(
      zoom(...steps, {type: 'jump', route: {...RUN_ROUTE, idea: 'i1', step: 's1'}}).remembered,
    ).toHaveLength(2);
  });

  test('up walks item, step, idea, run, then back to the Ideas', () => {
    const steps = [
      {type: 'open-idea', idea: 'i1'},
      {type: 'open-step', step: 's1'},
      {type: 'open-item', item: 't1'},
    ] as const;
    expect(zoom(...steps).route).toEqual({...RUN_ROUTE, idea: 'i1', step: 's1', item: 't1'});
    expect(zoom(...steps, {type: 'up'}).route).toEqual({...RUN_ROUTE, idea: 'i1', step: 's1'});
    expect(zoom(...steps, {type: 'up'}, {type: 'up'}).route).toEqual({...RUN_ROUTE, idea: 'i1'});
    expect(zoom(...steps, {type: 'up'}, {type: 'up'}, {type: 'up'}).route).toEqual(RUN_ROUTE);
    expect(zoom({type: 'mode', mode: 'timeline'}, {type: 'up'}).route).toEqual(RUN_ROUTE);
  });

  test('the mode keeps the level, and Now opens a live step in one zoom', () => {
    const opened = zoom({type: 'open-step', idea: 'i2', step: 's9'}, {type: 'mode', mode: 'logs'});
    expect(opened.route).toEqual({...RUN_ROUTE, mode: 'logs', idea: 'i2', step: 's9'});
    expect(opened.remembered).toEqual(['idea=i2&step=s9', 'mode=logs&idea=i2&step=s9']);
  });

  test('the Timeline and Agents faces replace an open idea, so the tab always changes the pane', () => {
    for (const mode of ['timeline', 'agents'] as const) {
      const opened = zoom({type: 'open-idea', idea: 'i1'}, {type: 'mode', mode});
      expect(opened.route).toEqual({...RUN_ROUTE, mode});
      expect(opened.remembered).toEqual(['idea=i1', `mode=${mode}`]);
    }
    const logs = zoom({type: 'open-idea', idea: 'i1'}, {type: 'mode', mode: 'logs'});
    expect(logs.route).toEqual({...RUN_ROUTE, mode: 'logs', idea: 'i1'});
  });

  test('an address that opens an idea under the Timeline face shows the idea', () => {
    expect(routeFromQuery('mode=timeline&idea=i1')).toEqual({...RUN_ROUTE, idea: 'i1'});
  });

  test('an agent’s turn goes up to the agent, then to the Agents face', () => {
    const turn = zoom({type: 'open-agent', agent: 'g1'}, {type: 'open-step', step: 't'});
    expect(turn.route).toEqual({...RUN_ROUTE, mode: 'agents', agent: 'g1', step: 't'});
    const up = zoom(
      {type: 'open-agent', agent: 'g1'},
      {type: 'open-step', step: 't'},
      {type: 'up'},
    );
    expect(up.route).toEqual({...RUN_ROUTE, mode: 'agents', agent: 'g1'});
    expect(routeUp(up.route)).toEqual({...RUN_ROUTE, mode: 'agents'});
  });
});

describe('the route in the address', () => {
  test('a route reads back as itself', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      for (let n = 0; n < 50; n += 1) {
        const route = randomRoute(random);
        expect(routeFromQuery(routeQuery(route))).toEqual(route);
        expect(routeFromQuery(`#${routeQuery(route)}`)).toEqual(route);
      }
    }
  });

  test('an address that names nothing, or levels out of order, falls back', () => {
    expect(routeFromQuery('')).toEqual(RUN_ROUTE);
    expect(routeFromQuery('mode=nonsense&step=s')).toEqual(RUN_ROUTE);
    expect(routeFromQuery('idea=%E0%A4%A&step=s')).toEqual(RUN_ROUTE);
    expect(routeFromQuery('idea=i&item=t')).toEqual({...RUN_ROUTE, idea: 'i'});
  });
});
