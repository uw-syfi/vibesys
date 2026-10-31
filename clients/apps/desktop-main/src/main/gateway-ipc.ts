/** Generic IPC composition. Authorization precedes provisioning or dialing. */
import {arch, platform} from 'node:os';
import {
  type ConnectionManager,
  GATEWAY_DIRECTORY,
  GatewayCalls,
  type GatewayDialer,
  gatewayArtifact,
  gatewayProcess,
  gatewayTarget,
  localGateway,
  provisionGateway,
  READ_METHOD,
  SshLink,
} from '@vibesys/connection';
import {type HostId, type HostKey, hostFromKey} from '@vibesys/hosts';
import {GATEWAY_CHANNEL, gatewayReply, parseGatewayRequest} from '@vibesys/ipc';
import {configuredHostCall, configuredRead, HOST_CALL_METHOD} from '@vibesys/runs/intents';
import type {ScheduleTimeout} from '@vibesys/transport';
import type {IpcMain} from 'electron';
import {servePort} from './port-ipc.js';
import type {WelcomeInvokeEvent} from './welcome-ipc.js';

export function createGatewayCalls(
  manager: ConnectionManager,
  resources: string,
  schedule: ScheduleTimeout,
  log: (message: string) => void,
): GatewayCalls {
  const calls = new GatewayCalls(
    key => gatewayFor(key, manager, resources, schedule, line => log(`${key}: ${line}`)),
    schedule,
    (host, diagnostic) => log(`${host} gateway call failed: ${diagnostic}`),
  );
  manager.onDisposing(() => calls.close());
  return calls;
}

export function installGatewayIpc(
  ipc: IpcMain,
  {
    calls,
    checkout,
    admits,
  }: {
    calls: GatewayCalls;
    checkout: (host: HostId) => Promise<string | null>;
    admits: (event: WelcomeInvokeEvent) => boolean;
  },
): void {
  servePort(ipc, GATEWAY_CHANNEL, admits, async (value, signal, send) => {
    const request = parseGatewayRequest(value);
    const receive = (bytes: Uint8Array): void => send(gatewayReply(bytes));
    if (request.kind === 'unary') {
      const bytes = await withCheckout(checkout, request);
      receive(await calls.unary(request.host, request.method, bytes, signal));
    } else await calls.stream(request.host, request.method, request.bytes, signal, receive);
  });
}

/** Host-call and read requests carry the checkout configured for their host. */
async function withCheckout(
  checkout: (host: HostId) => Promise<string | null>,
  request: {readonly host: HostKey; readonly method: string; readonly bytes: Uint8Array},
): Promise<Uint8Array> {
  if (request.method !== HOST_CALL_METHOD && request.method !== READ_METHOD) return request.bytes;
  const saved = await checkout(hostFromKey(request.host));
  return request.method === HOST_CALL_METHOD
    ? configuredHostCall(request.bytes, saved)
    : configuredRead(request.bytes, saved);
}

function gatewayFor(
  key: HostKey,
  manager: ConnectionManager,
  resources: string,
  schedule: ScheduleTimeout,
  log: (line: string) => void,
): GatewayDialer {
  const host = hostFromKey(key);
  return {
    dial: async (signal, force) => {
      if (host.kind === 'local')
        return localGateway(resources, gatewayTarget(`${platform()} ${arch()}`), signal, schedule);
      const lease = await manager.leaseLink(host);
      try {
        if (!(lease.link instanceof SshLink)) throw new Error('host has no SSH command link');
        await lease.link.establish(false, signal);
        await provisionGateway(
          lease.link,
          target => gatewayArtifact(resources, target),
          force,
          signal,
          schedule,
        );
        if (signal.aborted) throw new Error('gateway dial cancelled');
        const duplex = gatewayProcess(
          lease.link.spawnCommand(`exec "$HOME/${GATEWAY_DIRECTORY}/vibesysd" connect`),
          schedule,
          log,
          (code, tail) => lease.link.explain(code, tail, 'the gateway').message,
        );
        duplex.once('close', () => {
          void lease.release();
        });
        return duplex;
      } catch (error) {
        await lease.release();
        throw error;
      }
    },
  };
}
