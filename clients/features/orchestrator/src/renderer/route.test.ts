import {describe, expect, test} from 'bun:test';
import fc from 'fast-check';
import {initialRoute, type RouteAction, routeReducer} from './route.js';

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
      'timeline' as const,
      'cost' as const,
      'handoffs' as const,
      'ideas' as const,
    ),
  }),
  fc.record({type: fc.constant('pick-agent' as const), agentId: fc.constantFrom('a', 'b')}),
  fc.constant({type: 'close-agent' as const}),
);

describe('the route', () => {
  test('starts on the run list and opens a run on its timeline with no agent', () => {
    const route = routeReducer(initialRoute, {type: 'open-run', host: 'host-a', runId: 'run-1'});
    expect(route).toEqual({
      screen: 'run',
      host: 'host-a',
      runId: 'run-1',
      view: 'timeline',
      agentId: null,
    });
  });

  test('moves that need a run change nothing on the list', () => {
    fc.assert(
      fc.property(fc.constantFrom('timeline' as const, 'cost' as const), view => {
        expect(routeReducer(initialRoute, {type: 'show', view})).toBe(initialRoute);
        expect(routeReducer(initialRoute, {type: 'pick-agent', agentId: 'a'})).toBe(initialRoute);
      }),
    );
  });

  test('after any moves, closing the run returns to the list and the agent belongs to the run open', () => {
    fc.assert(
      fc.property(fc.array(action, {maxLength: 30}), moves => {
        const route = moves.reduce(routeReducer, initialRoute);
        expect(routeReducer(route, {type: 'close-run'})).toEqual(initialRoute);
        const reopened = routeReducer(route, {type: 'open-run', host: 'host-b', runId: 'run-2'});
        expect(reopened).toMatchObject({agentId: null, view: 'timeline'});
      }),
    );
  });
});
