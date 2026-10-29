/**
 * The machine-scoped Settings requests: what a machine offers as places to measure (its Slurm
 * clusters and its Modal token), testing, adding and removing a cluster, and each harness's login
 * on the machine. Each is one host helper method; the main process answers over the machine's
 * connection. A helper that predates a feature answers `unsupported` to its reads, so Settings can
 * say so instead of showing nothing.
 */
import {
  type AgentLogin,
  type ClusterTestFailed,
  type ClusterTestPassed,
  type ComputeAddClusterRequest,
  type ComputeSettings,
  type LoginStarted,
  type LoginUnavailable,
  parseHelperRequest,
  type SlurmClusterSettings,
} from '@vibesys/contracts/host-helper';
import {type HostId, type HostKey, hostFromKey} from '@vibesys/hosts';
import type {Offered} from './new-run.js';
import type {WelcomeResult} from './types.js';
import type {ChannelOf} from './values.js';

export const MACHINE_SETTINGS_CHANNELS = {
  compute: 'vibesys:welcome:compute',
  testCluster: 'vibesys:welcome:test-cluster',
  addCluster: 'vibesys:welcome:add-cluster',
  removeCluster: 'vibesys:welcome:remove-cluster',
  agentLogins: 'vibesys:welcome:agent-logins',
  startLogin: 'vibesys:welcome:start-login',
} as const;

export type {AgentLogin, ComputeSettings, SlurmClusterSettings};

/** What testing a cluster found. */
export type ClusterTest = ClusterTestPassed | ClusterTestFailed;

/** What starting a harness's own login did. */
export type LoginStart = LoginStarted | LoginUnavailable;

/** The welcome bridge's requests the machine-scoped Settings make. */
export interface MachineSettingsWelcomeBridge {
  /** The clusters and the Modal token `host` has set up, and the file it keeps them in. */
  compute(host: HostKey): Promise<WelcomeResult<Offered<ComputeSettings>>>;
  /** Try to reach `cluster` from `host` and read what its scheduler offers; saves nothing. */
  testCluster(host: HostKey, cluster: SlurmClusterSettings): Promise<WelcomeResult<ClusterTest>>;
  /** Save `cluster` on `host`, once it passes the test again. Answers with the settings after. */
  addCluster(host: HostKey, cluster: SlurmClusterSettings): Promise<WelcomeResult<ComputeSettings>>;
  /** Remove the cluster `name` from `host`'s own file. Answers with the settings after. */
  removeCluster(host: HostKey, name: string): Promise<WelcomeResult<ComputeSettings>>;
  /** Each vetted harness's login on `host`. */
  agentLogins(host: HostKey): Promise<WelcomeResult<Offered<readonly AgentLogin[]>>>;
  /** Start `provider`'s own login on `host`. */
  startLogin(host: HostKey, provider: string): Promise<WelcomeResult<LoginStart>>;
}

export type MachineSettingsRequest =
  | {readonly kind: 'compute'; readonly host: HostId}
  | {readonly kind: 'test-cluster'; readonly host: HostId; readonly cluster: SlurmClusterSettings}
  | {readonly kind: 'add-cluster'; readonly host: HostId; readonly cluster: SlurmClusterSettings}
  | {readonly kind: 'remove-cluster'; readonly host: HostId; readonly name: string}
  | {readonly kind: 'agent-logins'; readonly host: HostId}
  | {readonly kind: 'start-login'; readonly host: HostId; readonly provider: string};

const REQUEST_PREFIX = 'Invalid server helper request.';

/**
 * `value` as a cluster, by the helper contract's own schema (unknown keys, patterns and ranges
 * included), or an `Error` naming the first offending key as `cluster.<key>`.
 */
function cluster(value: unknown): SlurmClusterSettings {
  try {
    const request = parseHelperRequest(
      JSON.stringify({
        request_id: 'validate',
        deadline_ms: 1,
        type: 'compute.add_cluster',
        cluster: value,
      }),
    ) as ComputeAddClusterRequest;
    return request.cluster;
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new Error(
      message.startsWith(REQUEST_PREFIX) ? message.slice(REQUEST_PREFIX.length) : message,
    );
  }
}

function line(value: unknown, what: string): string {
  if (typeof value !== 'string' || value.trim() === '' || /[\r\n]/.test(value)) {
    throw new Error(`${what} must be one line of text`);
  }
  return value;
}

/** The typed request on `channel` with `args`; throws naming the first invalid value. */
export function parseMachineSettingsRequest(
  channel: ChannelOf<typeof MACHINE_SETTINGS_CHANNELS>,
  args: readonly unknown[],
): MachineSettingsRequest {
  const [first, second] = args;
  const host = hostFromKey(first);
  switch (channel) {
    case MACHINE_SETTINGS_CHANNELS.compute:
      return {kind: 'compute', host};
    case MACHINE_SETTINGS_CHANNELS.testCluster:
      return {kind: 'test-cluster', host, cluster: cluster(second)};
    case MACHINE_SETTINGS_CHANNELS.addCluster:
      return {kind: 'add-cluster', host, cluster: cluster(second)};
    case MACHINE_SETTINGS_CHANNELS.removeCluster:
      return {kind: 'remove-cluster', host, name: line(second, 'the cluster name')};
    case MACHINE_SETTINGS_CHANNELS.agentLogins:
      return {kind: 'agent-logins', host};
    case MACHINE_SETTINGS_CHANNELS.startLogin:
      return {kind: 'start-login', host, provider: line(second, 'the harness')};
  }
}
