/** Generic IPC composition. Authorization and per-host opt-in precede provisioning or dialing. */
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
  SshLink,
} from '@vibesys/connection';
import {type HostKey, hostFromKey, hostKey} from '@vibesys/hosts';
import {GATEWAY_CHANNEL, parseGatewayRequest} from '@vibesys/ipc';
import {configuredHostCall, HOST_CALL_METHOD} from '@vibesys/runs/intents';
import {type SettingsStore, settingsFor, withHostSettings} from '@vibesys/settings';
import type {ScheduleTimeout} from '@vibesys/transport';
import type {IpcMain} from 'electron';
import {rendererGeneration, type WelcomeInvokeEvent} from './welcome-ipc.js';

export function createGatewayCalls(
  manager: ConnectionManager,
  settings: SettingsStore,
  resources: string,
  schedule: ScheduleTimeout,
): GatewayCalls {
  const calls = new GatewayCalls(
    async key => {
      const {settings: file} = await settings.hosts();
      return settingsFor(file, hostFromKey(key))?.runTransportV2 === true;
    },
    key => gatewayFor(key, manager, resources, schedule),
    schedule,
  );
  manager.onDisposing(() => calls.close());
  return calls;
}

export function installGatewayIpc(
  ipc: IpcMain,
  {
    calls,
    settings,
    admits,
  }: {
    calls: GatewayCalls;
    settings: SettingsStore;
    admits: (event: WelcomeInvokeEvent) => boolean;
  },
): void {
  ipc.on(GATEWAY_CHANNEL, (event, value: unknown) => {
    const port = event.ports[0];
    if (port === undefined) return;
    const abort = new AbortController();
    let done = false;
    const finish = (error?: unknown): void => {
      if (done) return;
      done = true;
      abort.abort();
      event.sender.removeListener('destroyed', cancelled);
      event.sender.removeListener('did-start-navigation', cancelled);
      port.postMessage({end: true, ...(error === undefined ? {} : {error: String(error)})});
      port.close();
    };
    const cancelled = (): void => finish(new Error('gateway caller closed'));
    port.on('message', cancelled);
    port.on('close', cancelled);
    port.start();
    event.sender.once('destroyed', cancelled);
    event.sender.once('did-start-navigation', cancelled);
    void (async () => {
      authorize(event, admits);
      const request = parseGatewayRequest(value);
      const receive = (bytes: Uint8Array): void => {
        if (!done) port.postMessage({bytes});
      };
      if (request.kind === 'unary') {
        let bytes = request.bytes;
        if (request.method === HOST_CALL_METHOD) {
          const {settings: file} = await settings.hosts();
          bytes = configuredHostCall(
            bytes,
            settingsFor(file, hostFromKey(request.host))?.checkout ?? null,
          );
        }
        receive(await calls.unary(request.host, request.method, bytes, abort.signal));
      } else await calls.stream(request.host, request.method, request.bytes, abort.signal, receive);
    })().then(() => finish(), finish);
  });
}

function gatewayFor(
  key: HostKey,
  manager: ConnectionManager,
  resources: string,
  schedule: ScheduleTimeout,
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

function authorize(
  event: import('electron').IpcMainEvent,
  admits: (event: WelcomeInvokeEvent) => boolean,
): void {
  if (
    event.senderFrame !== event.sender.mainFrame ||
    !admits({
      senderId: event.sender.id,
      senderUrl: event.senderFrame?.url ?? null,
      rendererGeneration: rendererGeneration(event.senderFrame),
    })
  )
    throw new Error('not an app-owned shell document');
}

export function gatewaySetting(
  settings: SettingsStore,
  checkoutFor: (host: import('@vibesys/hosts').HostId) => Promise<string | null>,
  disable: (host: HostKey) => void,
) {
  return async (
    host: import('@vibesys/hosts').HostId,
    enabled: boolean | undefined,
  ): Promise<boolean> => {
    const {settings: file} = await settings.hosts();
    const saved = settingsFor(file, host);
    if (enabled === undefined) return saved?.runTransportV2 === true;
    const checkout = saved?.checkout ?? (await checkoutFor(host));
    if (checkout === null) throw new Error('Set a checkout first');
    await settings.updateHosts(current =>
      withHostSettings(current, host, {
        ...settingsFor(current, host),
        checkout,
        runTransportV2: enabled,
      }),
    );
    if (!enabled) disable(hostKey(host));
    return enabled;
  };
}
