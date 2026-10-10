import {describe, expect, test} from 'bun:test';
import {StopController, type StopDeps} from './stop-controller.js';
import {stopKey} from './stop-run.js';
import {FakeHost} from './testing/fake-host.js';

const HOST = 'ssh:gpu-box';
const TARGET = {
  host: HOST,
  hostLabel: 'gpu-box',
  instanceId: '000000000001',
  label: 'spsc',
} as const;
const KEY = stopKey(HOST, TARGET.instanceId);

interface World {
  readonly controller: StopController;
  readonly host: FakeHost;
  readonly asked: string[];
  readonly stops: {force: boolean}[];
  changes: number;
}

/** A controller over a `FakeHost` whose one server is the run `000000000001`. */
async function world(options: {stop?: StopDeps['stop']; answer?: boolean} = {}): Promise<World> {
  const host = new FakeHost();
  await host.startServer([]);
  const state: World = {
    host,
    asked: [],
    stops: [],
    changes: 0,
    controller: new StopController({
      stop:
        options.stop ??
        (async (_host, instanceId, force) => {
          state.stops.push({force});
          return host.stopInstance(instanceId);
        }),
      confirm: async text => {
        state.asked.push(text);
        return options.answer ?? true;
      },
      changed: () => {
        state.changes += 1;
      },
    }),
  };
  return state;
}

describe('StopController', () => {
  test('asks first, then stops the run on its host and shows it ended', async () => {
    const w = await world();
    await w.controller.request(TARGET, false);
    expect(w.asked).toEqual([
      'Stop run spsc on gpu-box? It stops at the next safe point and can be resumed.',
    ]);
    expect(w.stops).toEqual([{force: false}]);
    expect(w.controller.views()[KEY]?.phase).toBe('ended');
    expect((await w.host.stopInstance(TARGET.instanceId)).outcome).toBe('not_running');
  });

  test('a declined confirmation stops nothing and leaves no flow', async () => {
    const w = await world({answer: false});
    await w.controller.request(TARGET, false);
    expect(w.stops).toEqual([]);
    expect(w.controller.views()).toEqual({});
  });

  test('an accepted stop shows Stopping… until the registry or the connection says it ended', async () => {
    const w = await world({
      stop: async (_host, id) => ({id, outcome: 'stopping', route: 'control_socket'}),
    });
    await w.controller.request(TARGET, false);
    expect(w.controller.views()[KEY]).toMatchObject({phase: 'stopping', text: 'Stopping…'});
    // Still listed: still stopping, and a second request neither asks nor stops again.
    w.controller.observeRegistry(HOST, new Set([TARGET.instanceId]));
    await w.controller.request(TARGET, false);
    expect(w.asked).toHaveLength(1);
    expect(w.controller.views()[KEY]?.phase).toBe('stopping');
    // Another host's registry says nothing about this run.
    w.controller.observeRegistry('local', new Set());
    expect(w.controller.views()[KEY]?.phase).toBe('stopping');
    w.controller.observeRegistry(HOST, new Set());
    expect(w.controller.views()[KEY]?.phase).toBe('ended');
  });

  test('the connection ending ends an accepted stop', async () => {
    const w = await world({
      stop: async (_host, id) => ({id, outcome: 'stopping', route: 'signal'}),
    });
    await w.controller.request(TARGET, false);
    w.controller.runEnded(HOST, TARGET.instanceId);
    expect(w.controller.views()[KEY]?.phase).toBe('ended');
  });

  test('unsupported offers a force stop behind a second confirmation that passes force', async () => {
    const forces: boolean[] = [];
    const w = await world({
      stop: async (_host, id, force) => {
        forces.push(force);
        return {id, outcome: force ? 'stopped' : 'unsupported', route: null};
      },
    });
    await w.controller.request(TARGET, false);
    expect(w.controller.views()[KEY]).toMatchObject({phase: 'error', canForce: true});
    await w.controller.request(TARGET, true);
    expect(w.asked).toHaveLength(2);
    expect(w.asked[1]).toContain('Force stop run spsc on gpu-box');
    expect(forces).toEqual([false, true]);
    expect(w.controller.views()[KEY]?.phase).toBe('ended');
  });

  test('a stop command that cannot run is shown as the error and can be retried', async () => {
    const w = await world({
      stop: async () => {
        throw new Error('the host is closed');
      },
    });
    await w.controller.request(TARGET, false);
    expect(w.controller.views()[KEY]).toMatchObject({
      phase: 'error',
      text: 'the host is closed',
      canForce: false,
    });
    await w.controller.request(TARGET, false);
    expect(w.asked).toHaveLength(2);
  });

  test('every change is announced', async () => {
    const w = await world();
    await w.controller.request(TARGET, false);
    expect(w.changes).toBeGreaterThanOrEqual(3);
  });
});
