import {describe, expect, test} from 'bun:test';
import {checkMachine, type MachineAction} from '@vibesys/machine/testing';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {createActor} from 'xstate';
import {
  type Route,
  RUN_MODES,
  RUN_ROUTE,
  routeFromQuery,
  routeLevel,
  routeQuery,
  routeUp,
  type ZoomEvent,
  zoomMachine,
} from './zoom.js';

const IDS = ['a', 'b c', 'flow:w1/#1', 'x&y=z', '#hash'];

function randomRoute(random: Seeded): Route {
  const opened = random.int(3);
  const idea = opened === 1 ? random.pick(IDS) : null;
  const agent = opened === 2 ? random.pick(IDS) : null;
  const step = opened > 0 && random.bool() ? random.pick(IDS) : null;
  const item = step !== null && random.bool() ? random.pick(IDS) : null;
  return {mode: random.pick(RUN_MODES), idea, agent, step, item};
}

function randomEvent(random: Seeded): ZoomEvent {
  return random.pick<ZoomEvent>([
    {type: 'open-idea', idea: random.pick(IDS)},
    {type: 'open-agent', agent: random.pick(IDS)},
    {type: 'open-step', step: random.pick(IDS)},
    {type: 'open-step', step: random.pick(IDS), idea: random.pick(IDS)},
    {type: 'open-item', item: random.pick(IDS)},
    {type: 'mode', mode: random.pick(RUN_MODES)},
    {type: 'up'},
    {type: 'up'},
    {type: 'restore', route: randomRoute(random)},
  ]);
}

function zoom(...events: ZoomEvent[]): {route: Route; remembered: string[]} {
  const remembered: string[] = [];
  const actor = createActor(
    zoomMachine.provide({
      actions: {remember: ({context}) => remembered.push(routeQuery(context.route))},
    }),
  ).start();
  for (const event of events) actor.send(event);
  return {route: actor.getSnapshot().context.route, remembered};
}

describe('the zoom machine', () => {
  test('its state is the route’s level, and every zoom but a restore is remembered', () => {
    checkMachine({
      machine: zoomMachine,
      input: undefined,
      event: random => randomEvent(random),
      invariants: snapshot => {
        const level = routeLevel(snapshot.context.route);
        const state = level === 'opened' ? 'opened' : level;
        const route = snapshot.context.route;
        return [
          ...(snapshot.matches(state) ? [] : [`level ${level} in ${String(snapshot.value)}`]),
          ...(route.idea !== null && route.agent !== null ? ['an idea and an agent open'] : []),
          ...(route.item !== null && route.step === null ? ['an item without its step'] : []),
        ];
      },
      also: (before, event, after, actions: readonly MachineAction[]) => {
        const changed = before.context.route !== after.context.route;
        const remembered = actions.some(action => action.type === 'remember');
        if (event.type === 'restore') expect(remembered).toBe(false);
        else expect(remembered).toBe(changed);
      },
    });
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
