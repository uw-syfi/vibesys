import {describe, expect, test} from 'bun:test';
import {HostValidationError} from '@vibesys/hosts';
import {type GatewayPortBridge, gatewayEnd, gatewayReply, parseGatewayRequest} from '@vibesys/ipc';
import {
  FakeDaemon,
  FakeGateway,
  FixtureSource,
  generateScenario,
} from '@vibesys/orchestrator-fixtures';
import {sendMessageCommand} from '@vibesys/orchestrator-proto';
import {SourceError} from '@vibesys/orchestrator-source';
import {addressedHosts, liveHost} from './live.js';

/**
 * The preload's side of the bridge, in memory: requests arrive as the bytes a renderer sends,
 * are served by a fake gateway over a fixture daemon, and are answered as the bytes main sends.
 */
function bridgeOver(daemon: FakeDaemon, gateway = new FakeGateway(new FixtureSource(daemon))) {
  const bridge: GatewayPortBridge = {
    open(request, receive) {
      const call = parseGatewayRequest(request);
      if (call.kind === 'unary') {
        const unary = gateway.unary(call.host, call.method, call.bytes);
        unary.result.then(
          bytes => {
            receive(gatewayReply(bytes));
            receive(gatewayEnd());
          },
          (error: Error & {status?: number}) => receive(gatewayEnd(error.message, error.status)),
        );
        return {cancel: unary.cancel};
      }
      return gateway.stream(
        call.host,
        call.method,
        call.bytes,
        bytes => receive(gatewayReply(bytes)),
        (error, status) => receive(gatewayEnd(error, status)),
      );
    },
  };
  return bridge;
}

const scenario = generateScenario({seed: 5, subagents: 3, live: false});
const open = new AbortController().signal;

describe('the hosts a window address names', () => {
  test('each is checked as a host, and a repeated one is shown once', () => {
    const hosts = addressedHosts(new URLSearchParams('host=local&host=ssh:host-a&host=local'));
    expect(hosts).toEqual([{kind: 'local'}, {kind: 'ssh', alias: 'host-a'}]);
  });

  test('an address with no host names none, and one that names no host is refused by name', () => {
    expect(addressedHosts(new URLSearchParams('fixture=long-run'))).toEqual([]);
    expect(() => addressedHosts(new URLSearchParams('host=-oProxyCommand'))).toThrow(
      HostValidationError,
    );
  });
});

describe('a live host over the desktop bridge', () => {
  test('its run stream starts with the snapshot the daemon holds', async () => {
    const ports = liveHost(
      {kind: 'ssh', alias: 'host-a'},
      bridgeOver(new FakeDaemon(scenario)),
    ).ports;
    const watch = ports.openRuns(0n);
    const first = await watch.frames[Symbol.asyncIterator]().next();
    watch.close();
    expect(first.value?.case).toBe('snapshot');
    expect(Object.keys(first.value?.value?.runs ?? {}).length).toBeGreaterThan(0);
  });

  test('a message is answered with a receipt, and the same id with the same one', async () => {
    const daemon = new FakeDaemon(scenario);
    const agent = daemon.snapshot().agents.find(candidate => candidate.status === 2);
    const ports = liveHost({kind: 'local'}, bridgeOver(daemon)).ports;
    const command = sendMessageCommand('cmd-live', agent?.id ?? 'nobody', 'hello');
    const first = await ports.send(command, open);
    expect(first.commandId).toBe('cmd-live');
    expect(await ports.send(command, open)).toEqual(first);
  });

  test('a host whose daemon cannot serve says why, as a lost connection naming the reason', async () => {
    const refuse = {status: 14, message: 'orchestrator held by pid 41 (version dev)'};
    const gateway = new FakeGateway(new FixtureSource(new FakeDaemon(scenario)), {refuse});
    const ports = liveHost({kind: 'local'}, bridgeOver(new FakeDaemon(scenario), gateway)).ports;
    const next = ports.openRuns(0n).frames[Symbol.asyncIterator]().next();
    await expect(next).rejects.toMatchObject({
      kind: 'lost',
      message: 'orchestrator held by pid 41 (version dev)',
    });
  });

  test('a page with no desktop bridge says so on every request', async () => {
    const ports = liveHost({kind: 'local'}, undefined).ports;
    await expect(ports.openRuns(0n).frames[Symbol.asyncIterator]().next()).rejects.toBeInstanceOf(
      SourceError,
    );
    await expect(ports.listPlans('run', open)).rejects.toThrow(
      'not running inside the VibeSys app',
    );
  });
});
