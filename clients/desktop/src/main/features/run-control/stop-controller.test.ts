import {describe, expect, test} from 'bun:test';
import {FakeClock} from '@vibesys/backend-client/testing';
import {stopKey} from '../../../common/stop-run.js';
import {DEFAULT_CONNECTION_CONFIG, HostConnection} from '../../platform/connection/index.js';
import {FakeLink, record} from '../../platform/connection/testing/fake-helper.js';
import {StopController, type StopDeps} from './stop-controller.js';

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
  readonly host: HostConnection;
  readonly asked: string[];
  readonly stops: {force: boolean}[];
  changes: number;
}

/** A controller over a FakeHelper host whose one server is the run `000000000001`. */
async function world(options: {stop?: StopDeps['stop']; answer?: boolean} = {}): Promise<World> {
  const link = new FakeLink();
  link.helper.publish(record(TARGET.instanceId, 'run-1'));
  const host = new HostConnection({
    link,
    appVersion: 'test',
    config: DEFAULT_CONNECTION_CONFIG,
    scheduleTimeout: new FakeClock().scheduleTimeout,
    log: () => {},
  });
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
          const reply = await host.request({type: 'run.stop', instance_id: instanceId, force});
          return {kind: 'answered', result: reply.result};
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
    const again = await w.host.request({
      type: 'run.stop',
      instance_id: TARGET.instanceId,
      force: false,
    });
    expect(again.result.outcome).toBe('not_running');
    await w.host[Symbol.asyncDispose]();
  });

  test('a declined confirmation stops nothing and leaves no flow', async () => {
    const w = await world({answer: false});
    await w.controller.request(TARGET, false);
    expect(w.stops).toEqual([]);
    expect(w.controller.views()).toEqual({});
  });

  test('a confirmed stop uses the same flow without asking again', async () => {
    const w = await world();
    expect(await w.controller.stopConfirmed(TARGET)).toEqual({kind: 'complete'});
    expect(w.asked).toEqual([]);
    expect(w.stops).toEqual([{force: false}]);
    expect(w.controller.views()[KEY]?.phase).toBe('ended');
    await w.host[Symbol.asyncDispose]();
  });

  test('a confirmed stop returns the failure already recorded in its view', async () => {
    const w = await world({
      stop: async (_host, id) => ({
        kind: 'answered',
        result: {id, outcome: 'unsupported', route: null},
      }),
    });
    expect(await w.controller.stopConfirmed(TARGET)).toEqual({
      kind: 'failed',
      message: 'This run cannot be stopped gracefully from here.',
    });
    expect(w.controller.views()[KEY]).toMatchObject({phase: 'error', canForce: true});
    await w.host[Symbol.asyncDispose]();
  });

  test('a confirmed stop withdrawn before the host write leaves no stop flow', async () => {
    const w = await world({stop: async () => ({kind: 'not-written'})});
    expect(await w.controller.stopConfirmed(TARGET)).toEqual({kind: 'withdrawn'});
    expect(w.controller.views()).toEqual({});
    expect(w.stops).toEqual([]);
    await w.host[Symbol.asyncDispose]();
  });

  test('a failure after the stop write is preserved when ownership changes before its reply', async () => {
    let current = true;
    const w = await world({
      stop: async () => {
        current = false;
        return {kind: 'failed', written: true, message: 'the written stop lost its reply'};
      },
    });

    expect(await w.controller.stopConfirmed(TARGET, () => current)).toEqual({
      kind: 'failed',
      message: 'the written stop lost its reply',
    });
    expect(w.controller.views()[KEY]).toMatchObject({
      phase: 'error',
      text: 'the written stop lost its reply',
    });
    await w.host[Symbol.asyncDispose]();
  });

  test('an accepted stop shows Stopping… until the registry or the connection says it ended', async () => {
    const w = await world({
      stop: async (_host, id) => ({
        kind: 'answered',
        result: {id, outcome: 'stopping', route: 'control_socket'},
      }),
    });
    await w.controller.request(TARGET, false);
    expect(w.controller.views()[KEY]).toMatchObject({phase: 'stopping', text: 'Stopping…'});
    // Still listed: still stopping, and a second request does nothing.
    w.controller.observeLive(HOST, new Set([TARGET.instanceId]));
    await w.controller.request(TARGET, false);
    expect(w.asked).toHaveLength(1);
    expect(w.controller.views()[KEY]?.phase).toBe('stopping');
    // Another host's registry says nothing about this run.
    w.controller.observeLive('local', new Set());
    expect(w.controller.views()[KEY]?.phase).toBe('stopping');
    w.controller.observeLive(HOST, new Set());
    expect(w.controller.views()[KEY]?.phase).toBe('ended');
  });

  test('the connection ending ends an accepted stop', async () => {
    const w = await world({
      stop: async (_host, id) => ({
        kind: 'answered',
        result: {id, outcome: 'stopping', route: 'signal'},
      }),
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
        return {
          kind: 'answered',
          result: {id, outcome: force ? 'stopped' : 'unsupported', route: null},
        };
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
