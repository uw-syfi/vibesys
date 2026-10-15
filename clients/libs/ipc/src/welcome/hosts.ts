/**
 * The hosts feature's welcome requests: the host picker, a host's checkout and sign-in, path
 * completion, and the inspector's run inspection and logs. The channel names, what the view's bridge sends on them, and how the main process
 * parses what arrives.
 */
import {
  type HostId,
  type HostKey,
  type HostSummary,
  hostFromKey,
  type InstanceInspection,
  validHostPath,
} from '@vibesys/hosts';
import type {LogListOutcome, LogReadOutcome} from '@vibesys/logs';
import {isInstanceId} from '@vibesys/runs';
import {parseRunLogsRequest, type RunLogsRequest} from '../run-logs.js';
import type {PathCompletion, WelcomeResult} from './types.js';
import {type ChannelOf, operationToken} from './values.js';

export const HOSTS_CHANNELS = {
  hosts: 'vibesys:welcome:hosts',
  host: 'vibesys:welcome:host',
  setCheckout: 'vibesys:welcome:set-checkout',
  cancelCheckout: 'vibesys:welcome:cancel-checkout',
  cancelInspection: 'vibesys:welcome:cancel-inspection',
  signIn: 'vibesys:welcome:sign-in',
  complete: 'vibesys:welcome:complete',
  inspect: 'vibesys:welcome:inspect',
  logs: 'vibesys:welcome:logs',
  releaseLogs: 'vibesys:welcome:release-logs',
} as const;

/** The welcome bridge's requests the hosts feature makes. */
export interface HostsWelcomeBridge {
  hosts(): Promise<WelcomeResult<string[]>>;
  host(host: HostKey): Promise<WelcomeResult<HostSummary>>;
  /** Change a checkout under `change`, so losing renderer intent can cancel the main-process work. */
  setCheckout(host: HostKey, path: string, change: number): Promise<WelcomeResult<string>>;
  cancelCheckout(change: number): Promise<WelcomeResult<null>>;
  /** Revoke a still-pending inspection before it can persist its diagnostic locator. */
  cancelInspection(inspection: number): Promise<WelcomeResult<null>>;
  signIn(host: HostKey): Promise<WelcomeResult<null>>;
  /** The directories that complete `directory` (a path ending in `/`) on `host`. */
  complete(host: HostKey, directory: string): Promise<WelcomeResult<PathCompletion>>;
  inspect(
    host: HostKey,
    instance: string,
    inspection: number,
  ): Promise<WelcomeResult<InstanceInspection>>;
  /**
   * One logs request about `instance` on `host`, over the host's logs channel, which the main
   * process holds for this view until `releaseLogs` (`docs/contributing/desktop-connection.md`,
   * "Logs"). A read must name `instance`.
   */
  logs(
    host: HostKey,
    instance: string,
    request: RunLogsRequest,
  ): Promise<WelcomeResult<LogListOutcome | LogReadOutcome>>;
  /** Let the host's logs channel go: the view shows no logs now. */
  releaseLogs(): Promise<WelcomeResult<null>>;
}

export type HostsRequest =
  | {readonly kind: 'hosts'}
  | {readonly kind: 'host'; readonly host: HostId}
  | {
      readonly kind: 'set-checkout';
      readonly host: HostId;
      /** Validated by the checkout's owner, which reports a bad path in the host panel. */
      readonly path: unknown;
      readonly change: number;
    }
  | {readonly kind: 'cancel-checkout'; readonly change: number}
  | {readonly kind: 'cancel-inspection'; readonly inspection: number}
  | {readonly kind: 'sign-in'; readonly host: HostId}
  /** List `directory` (a path ending in `/`) for completion. */
  | {readonly kind: 'complete'; readonly host: HostId; readonly directory: string}
  | {
      readonly kind: 'inspect';
      readonly host: HostId;
      readonly instance: string;
      /** Renderer-local operation sequence, paired with its document identity by main. */
      readonly inspection: number;
    }
  | {
      readonly kind: 'logs';
      readonly host: HostId;
      readonly instance: string;
      readonly request: RunLogsRequest;
    }
  | {readonly kind: 'release-logs'};

/** A directory to list: a host path, as the listing's prefix (ending in `/`). */
function directory(value: unknown): string {
  const path = validHostPath(value, 'the directory to complete');
  return path.endsWith('/') ? path : `${path}/`;
}

function instance(value: unknown): string {
  if (!isInstanceId(value)) {
    throw new Error('the instance to inspect must be a 12-digit instance id');
  }
  return value;
}

/** A logs request about `instanceId`: a listing, or a read that names it. */
function logsRequest(value: unknown, instanceId: string): RunLogsRequest {
  const request = parseRunLogsRequest(value);
  if (request === null) throw new Error('the logs request is not a listing or a read');
  if (request.kind === 'read' && request.read.instance_id !== instanceId) {
    throw new Error(`the logs read names ${request.read.instance_id}, not ${instanceId}`);
  }
  return request;
}

/** The typed request on `channel` with `args`; throws naming the first invalid value. */
export function parseHostsRequest(
  channel: ChannelOf<typeof HOSTS_CHANNELS>,
  args: readonly unknown[],
): HostsRequest {
  const [first, second, third] = args;
  switch (channel) {
    case HOSTS_CHANNELS.hosts:
      return {kind: 'hosts'};
    case HOSTS_CHANNELS.host:
      return {kind: 'host', host: hostFromKey(first)};
    case HOSTS_CHANNELS.setCheckout:
      return {
        kind: 'set-checkout',
        host: hostFromKey(first),
        path: second,
        change: operationToken(third, 'checkout change'),
      };
    case HOSTS_CHANNELS.cancelCheckout:
      return {kind: 'cancel-checkout', change: operationToken(first, 'checkout change')};
    case HOSTS_CHANNELS.cancelInspection:
      return {kind: 'cancel-inspection', inspection: operationToken(first, 'inspection')};
    case HOSTS_CHANNELS.signIn:
      return {kind: 'sign-in', host: hostFromKey(first)};
    case HOSTS_CHANNELS.complete:
      return {kind: 'complete', host: hostFromKey(first), directory: directory(second)};
    case HOSTS_CHANNELS.inspect:
      return {
        kind: 'inspect',
        host: hostFromKey(first),
        instance: instance(second),
        inspection: operationToken(third, 'inspection'),
      };
    case HOSTS_CHANNELS.logs: {
      const id = instance(second);
      return {
        kind: 'logs',
        host: hostFromKey(first),
        instance: id,
        request: logsRequest(third, id),
      };
    }
    case HOSTS_CHANNELS.releaseLogs:
      return {kind: 'release-logs'};
  }
}

export type WelcomeInspection = InstanceInspection;
