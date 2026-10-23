/**
 * Cross-process composition with only message delivery and the socket session replaced. A
 * helper for tests, not a test file: the test runner collects only `*.test.ts`.
 */
import {EventEmitter} from 'node:events';
import {createRequire} from 'node:module';
import {PassThrough} from 'node:stream';
import {create, toBinary} from '@bufbuild/protobuf';
import {GatewayCalls, type GatewaySessionFactory, HELLO_METHOD} from '@vibesys/connection';
import {FakeGatewaySession, type GatewayWireCall, Schedule} from '@vibesys/connection/testing';
import {
  type GatewayPreload,
  type GatewayRequest,
  gatewayBridge,
  gatewayTransport,
} from '@vibesys/ipc';
import {FakeGatewayPort} from '@vibesys/ipc/testing';
import {API_SCHEMA_HASH, HelloResponseSchema} from '@vibesys/proto';
import {SettingsStore} from '@vibesys/settings';
import {FakeSettingsFilesystem} from '@vibesys/settings/testing';
import {FakeClock} from '@vibesys/transport/testing';
import type {IpcMainEvent} from 'electron';
import {installGatewayIpc} from '../main/gateway-ipc.js';

const createPreload = createRequire(import.meta.url)(
  '../preload/welcome.cjs',
) as typeof import('../preload/welcome.cjs');

export function gatewayWorld(seed: number, request: (call: GatewayWireCall) => void) {
  const clock = new FakeClock();
  const schedule = new Schedule(seed, clock);
  const session: GatewaySessionFactory = (duplex, _timers, lost) =>
    new FakeGatewaySession(
      duplex,
      schedule,
      call => {
        if (call.method === HELLO_METHOD) {
          call.message(
            toBinary(
              HelloResponseSchema,
              create(HelloResponseSchema, {
                apiSchemaHash: API_SCHEMA_HASH,
                gatewayVersion: 'test',
              }),
            ),
          );
          call.trailers('0', '');
        } else request(call);
      },
      clock.schedule,
      lost,
    );
  const calls = new GatewayCalls(
    async () => true,
    () => ({dial: async () => new PassThrough(), session}),
    clock.schedule,
  );
  let listener: (event: IpcMainEvent, value: unknown) => void = () => {
    throw new Error('IPC not installed');
  };
  const settings = new SettingsStore({
    settingsPath: '/settings/hosts.json',
    recentPath: '/settings/recent.json',
    preferencesPath: '/settings/preferences.json',
    filesystem: new FakeSettingsFilesystem(),
    log: () => {},
  });
  installGatewayIpc(
    {
      on: (_channel, receive) => {
        listener = receive;
      },
    },
    {calls, settings, admits: () => true},
  );
  const sender = Object.assign(new EventEmitter(), {
    id: 1,
    mainFrame: {url: 'app://vibesys/index.html', processId: 1, routingId: 1},
  });
  const ports: FakeGatewayPort[] = [];
  const preload = createPreload.gateway({
    newChannel: () => {
      const {page, main} = FakeGatewayPort.pair(
        (lane, run) => schedule.post(lane, run),
        structuredClone,
      );
      ports.push(page);
      // test-isolation: the Fake ports implement the subset of MessagePort the gateway code uses.
      return {port1: page, port2: main as unknown as MessagePort};
    },
    open: (request: GatewayRequest, port) => {
      // test-isolation: Electron owns a much larger event, but gateway IPC reads only these fields.
      listener(
        {ports: [port], sender, senderFrame: sender.mainFrame} as unknown as IpcMainEvent,
        request,
      );
    },
  });
  const bridge = gatewayBridge(acrossContextBridge(preload));
  return {
    schedule,
    clock,
    bridge,
    sender,
    ports,
    settings,
    transport: gatewayTransport(bridge, 'local'),
    close: () => {
      calls.close();
      for (const port of ports) port.close();
    },
  };
}

/**
 * What the page sees of the preload's bridge. Electron's context bridge copies values and drops
 * an Error's own properties, keeping its message: a refusal that travels as an Error loses its
 * status, which is why the preload resolves with a value (e2e/gateway.spec.ts checks it on a
 * real window).
 */
function acrossContextBridge(preload: GatewayPreload): GatewayPreload {
  return {
    unary: (host, method, bytes) => {
      const call = preload.unary(host, method, bytes);
      return {
        result: call.result.then(
          reply => structuredClone(reply),
          (error: unknown) => {
            throw new Error(error instanceof Error ? error.message : String(error));
          },
        ),
        cancel: () => call.cancel(),
      };
    },
    stream: (host, method, bytes, receive, ended) =>
      preload.stream(host, method, bytes, receive, ended),
  };
}
