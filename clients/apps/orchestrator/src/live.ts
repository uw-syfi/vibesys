/**
 * The hosts the window shows when it is opened for real hosts: `?host=local` or `?host=ssh:<alias>`
 * (repeat it for more). Each host is read through the desktop app's gateway bridge, the one
 * connection main keeps to that host's `vibesysd`; this page opens no connection of its own.
 */
import {type HostId, type HostKey, hostFromKey, hostKey} from '@vibesys/hosts';
import {type GatewayPortBridge, gatewayBridge} from '@vibesys/ipc';
import {GatewaySource, type HostPorts, portsOf, SourceError} from '@vibesys/orchestrator-source';

export interface LiveHost {
  readonly host: HostId;
  readonly ports: HostPorts;
}

/** The hosts a window address names, each checked as a host; an address naming none gives []. */
export function addressedHosts(params: URLSearchParams): HostId[] {
  const seen = new Set<HostKey>();
  const hosts: HostId[] = [];
  for (const key of params.getAll('host')) {
    const host = hostFromKey(key);
    if (seen.has(hostKey(host))) continue;
    seen.add(hostKey(host));
    hosts.push(host);
  }
  return hosts;
}

/**
 * Ports for `host` over `bridge`. A page that is not inside the desktop app has no bridge, and
 * says so as a lost connection with that reason, on every screen's banner, instead of failing
 * blank.
 */
export function liveHost(host: HostId, bridge: GatewayPortBridge | undefined): LiveHost {
  if (bridge === undefined) return {host, ports: noBridge()};
  return {host, ports: portsOf(new GatewaySource(gatewayBridge(bridge), hostKey(host)))};
}

const NO_BRIDGE = 'this page is not running inside the VibeSys app, so it has no connection';

function noBridge(): HostPorts {
  const lost = new SourceError('lost', NO_BRIDGE);
  const failing = {
    frames: {[Symbol.asyncIterator]: () => ({next: () => Promise.reject(lost)})},
    close: () => undefined,
  };
  return {
    openRuns: () => failing,
    openAgent: () => failing,
    listItems: () => Promise.reject(lost),
    listPlans: () => Promise.reject(lost),
    send: () => Promise.reject(lost),
    close: () => undefined,
  };
}
