import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {costTarget, initialRoute, type RouteAction, routeReducer} from './route.js';

const action: fc.Arbitrary<RouteAction> = fc.oneof(
  fc.record({
    type: fc.constant('open-run' as const),
    host: fc.constantFrom('host-a', 'host-b'),
    runId: fc.constantFrom('run-1', 'run-2'),
  }),
  fc.constant({type: 'close-run' as const}),
  fc.record({
    type: fc.constant('show' as const),
    view: fc.constantFrom(
      'report' as const,
      'agents' as const,
      'cost' as const,
      'catch-up' as const,
    ),
  }),
  fc.record({type: fc.constant('pick-agent' as const), agentId: fc.constantFrom('a', 'b')}),
  fc.constant({type: 'close-agent' as const}),
);

describe('the route', () => {
  it('starts on the run list and opens a run on its report with no agent', () => {
    const route = routeReducer(initialRoute, {type: 'open-run', host: 'host-a', runId: 'run-1'});
    expect(route).toEqual({
      screen: 'run',
      host: 'host-a',
      runId: 'run-1',
      view: 'report',
      agentId: null,
    });
  });

  it('moves that need a run change nothing on the list', () => {
    fc.assert(
      fc.property(fc.constantFrom('agents' as const, 'cost' as const), view => {
        expect(routeReducer(initialRoute, {type: 'show', view})).toBe(initialRoute);
        expect(routeReducer(initialRoute, {type: 'pick-agent', agentId: 'a'})).toBe(initialRoute);
      }),
    );
  });

  it('after any moves, closing the run returns to the list and the agent belongs to the run open', () => {
    fc.assert(
      fc.property(fc.array(action, {maxLength: 30}), moves => {
        const route = moves.reduce(routeReducer, initialRoute);
        expect(routeReducer(route, {type: 'close-run'})).toEqual(initialRoute);
        const reopened = routeReducer(route, {type: 'open-run', host: 'host-b', runId: 'run-2'});
        expect(reopened).toMatchObject({agentId: null, view: 'report'});
      }),
    );
  });
});

describe('which cost stream is followed', () => {
  it('is the run whose report or Cost view is showing, and none on any other screen', () => {
    fc.assert(
      fc.property(fc.array(action, {maxLength: 30}), moves => {
        const route = moves.reduce(routeReducer, initialRoute);
        const target = costTarget(route);
        const shown = route.screen === 'run' && (route.view === 'cost' || route.view === 'report');
        if (route.screen === 'run' && shown && route.agentId === null) {
          expect(target).toEqual({host: route.host, runId: route.runId});
        } else {
          expect(target).toBeNull();
        }
      }),
    );
  });

  it('opens when the Cost view is picked, and closes when an agent covers it or the run closes', () => {
    const open = routeReducer(initialRoute, {type: 'open-run', host: 'host-a', runId: 'run-1'});
    // A run opens on its report, which shows the spend, so the stream opens with it.
    expect(costTarget(open)).toEqual({host: 'host-a', runId: 'run-1'});
    expect(costTarget(routeReducer(open, {type: 'show', view: 'agents'}))).toBeNull();
    const cost = routeReducer(open, {type: 'show', view: 'cost'});
    expect(costTarget(cost)).toEqual({host: 'host-a', runId: 'run-1'});
    expect(costTarget(routeReducer(cost, {type: 'pick-agent', agentId: 'a'}))).toBeNull();
    expect(costTarget(routeReducer(cost, {type: 'close-run'}))).toBeNull();
    expect(costTarget(routeReducer(cost, {type: 'show', view: 'agents'}))).toBeNull();
  });

  it('opens an agent under the agents view, and any page of the run closes it', () => {
    fc.assert(
      fc.property(fc.array(action, {maxLength: 30}), moves => {
        const route = moves.reduce(routeReducer, initialRoute);
        if (route.screen !== 'run') return;
        const picked = routeReducer(route, {type: 'pick-agent', agentId: 'a'});
        expect(picked).toMatchObject({view: 'agents', agentId: 'a'});
        // The agent's page is never under another view, and a page of the run is never an agent's.
        for (const view of ['report', 'agents', 'cost', 'catch-up'] as const) {
          expect(routeReducer(picked, {type: 'show', view})).toMatchObject({view, agentId: null});
        }
        expect(routeReducer(picked, {type: 'close-agent'})).toMatchObject({
          view: 'agents',
          agentId: null,
        });
      }),
    );
  });
});
