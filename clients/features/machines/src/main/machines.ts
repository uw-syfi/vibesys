/**
 * The machines in the main process: which machines are saved (`hosts.json`, through the settings
 * store), what one machine's facts say now (its connection's state, then `machine.describe`), what
 * a path on a machine holds (`checkout.inspect`), and removing one. A machine's status is never
 * stored: it follows from these facts each time they are read. Saving a checkout, signing in and
 * completing paths are the hosts feature's (its `setCheckout` checks a checkout by starting its
 * helper there before it saves it).
 */
import {ConnectionError, type ConnectionLease, HelperRequestError} from '@vibesys/connection';
import {
  type HostId,
  hostFromKey,
  hostKey,
  hostLabel,
  missingRequiredFeatures,
} from '@vibesys/hosts';
import type {
  AgentLogin,
  CheckoutInspection,
  ClusterTest,
  ComputeSettings,
  LoginStart,
  MachineEntry,
  MachineReport,
  Offered,
  SlurmClusterSettings,
} from '@vibesys/ipc';
import type {SettingsStore} from '@vibesys/settings';

export interface MachinesMainDeps {
  /** Read the saved hosts; their owner performs all mutations. */
  readonly settings: Pick<SettingsStore, 'hosts'>;
  /** The hosts owner removes one host after fencing its checkout mutations. */
  readonly removeHost: (id: HostId) => Promise<void>;
  /** The checkout `id` runs VibeSys from (This Mac: the app's own unless one is saved). */
  readonly checkout: (id: HostId) => Promise<string | null>;
  /** Lease `id`'s one connection, from its saved checkout. */
  readonly connection: (id: HostId) => Promise<ConnectionLease>;
}

const LOCAL: HostId = {kind: 'local'};

/** What a connection that could not become ready says about its machine. */
function unreachable(error: unknown): MachineReport {
  const detail = error instanceof Error ? error.message : String(error);
  if (!(error instanceof ConnectionError)) return {kind: 'unavailable', detail};
  if (error.reason === 'auth') return {kind: 'sign-in', detail};
  if (error.reason === 'incompatible' && error.checkout === 'missing') {
    return {kind: 'checkout-not-found', detail};
  }
  return {kind: 'unavailable', detail};
}

function notNegotiated(error: unknown): boolean {
  return error instanceof HelperRequestError && error.code === 'feature_not_negotiated';
}

/** A failure the user reads: what was being done on which machine, then the helper's own words. */
function explained(error: unknown, doing: string, id: HostId): Error {
  const detail = error instanceof Error ? error.message : String(error);
  return new Error(`Could not ${doing} on ${hostLabel(id)}: ${detail}`);
}

export class MachinesMain {
  readonly #deps: MachinesMainDeps;

  constructor(deps: MachinesMainDeps) {
    this.#deps = deps;
  }

  /** This Mac, then every saved host by label, each with its checkout. */
  async machines(): Promise<MachineEntry[]> {
    const {settings} = await this.#deps.settings.hosts();
    const saved = Object.entries(settings.hosts)
      .filter(([key]) => key !== hostKey(LOCAL))
      .map(([key, each]) => ({
        host: key,
        label: hostLabel(hostFromKey(key)),
        checkout: each.checkout,
      }))
      .sort((left, right) => left.label.localeCompare(right.label));
    const local = await this.#deps.checkout(LOCAL);
    const here =
      local === null ? [] : [{host: hostKey(LOCAL), label: hostLabel(LOCAL), checkout: local}];
    return [...here, ...saved];
  }

  /** What `id`'s facts say now: its connection's state, then what its helper describes. */
  async report(id: HostId): Promise<MachineReport> {
    let lease: ConnectionLease;
    try {
      lease = await this.#deps.connection(id);
    } catch (error) {
      return unreachable(error);
    }
    await using held = lease;
    try {
      await held.connection.ready();
    } catch (error) {
      return unreachable(error);
    }
    const kind =
      missingRequiredFeatures(held.connection.features).length === 0 ? 'ready' : 'needs-update';
    try {
      const reply = await held.connection.request({type: 'machine.describe'});
      return {kind, facts: reply.machine};
    } catch (error) {
      if (notNegotiated(error)) return {kind, facts: null};
      return unreachable(error);
    }
  }

  /** What `path` on `id` holds, by its helper's own inspection. */
  async inspect(id: HostId, path: string): Promise<CheckoutInspection> {
    await using lease = await this.#deps.connection(id);
    try {
      const reply = await lease.connection.request({type: 'checkout.inspect', path});
      return reply.inspection;
    } catch (error) {
      if (notNegotiated(error)) return {state: 'unsupported'};
      throw error instanceof HelperRequestError
        ? new Error(`Could not inspect the folder: ${error.message}`)
        : error;
    }
  }

  /**
   * What `id` has set up as places to measure (its Slurm clusters and Modal token), or that its
   * helper predates machine compute settings.
   */
  async compute(id: HostId): Promise<Offered<ComputeSettings>> {
    return this.#offered(id, 'read the clusters and clouds', async connection => {
      const reply = await connection.request({type: 'compute.list'});
      return reply.settings;
    });
  }

  /** Try `cluster` from `id`: reach it, ask its scheduler, check its workspace. Saves nothing. */
  async testCluster(id: HostId, cluster: SlurmClusterSettings): Promise<ClusterTest> {
    return this.#changed(id, 'test the cluster', async connection => {
      const reply = await connection.request({type: 'compute.test_cluster', cluster});
      return reply.result;
    });
  }

  /** Save `cluster` on `id`; its helper tests it again first. Answers the settings after. */
  async addCluster(id: HostId, cluster: SlurmClusterSettings): Promise<ComputeSettings> {
    return this.#changed(id, 'save the cluster', async connection => {
      const reply = await connection.request({type: 'compute.add_cluster', cluster});
      return reply.settings;
    });
  }

  /** Remove the cluster `name` from `id`'s own file. Answers the settings after. */
  async removeCluster(id: HostId, name: string): Promise<ComputeSettings> {
    return this.#changed(id, 'remove the cluster', async connection => {
      const reply = await connection.request({type: 'compute.remove_cluster', name});
      return reply.settings;
    });
  }

  /** Each vetted harness's login on `id`, or that its helper predates login state. */
  async agentLogins(id: HostId): Promise<Offered<readonly AgentLogin[]>> {
    return this.#offered(id, 'read the agent logins', async connection => {
      const reply = await connection.request({type: 'agent_logins.list'});
      return reply.logins;
    });
  }

  /** Start `provider`'s own login on `id`. */
  async startLogin(id: HostId, provider: string): Promise<LoginStart> {
    return this.#changed(id, 'start the login', async connection => {
      const reply = await connection.request({type: 'agent_logins.start', provider});
      return reply.result;
    });
  }

  /** `read` over `id`'s connection, or `unsupported` from a helper that did not negotiate it. */
  async #offered<T>(
    id: HostId,
    doing: string,
    read: (connection: ConnectionLease['connection']) => Promise<T>,
  ): Promise<Offered<T>> {
    try {
      await using lease = await this.#deps.connection(id);
      return {kind: 'offered', value: await read(lease.connection)};
    } catch (error) {
      if (notNegotiated(error)) return {kind: 'unsupported'};
      throw explained(error, doing, id);
    }
  }

  /** `change` over `id`'s connection; a failure is worded for the user, a missing feature too. */
  async #changed<T>(
    id: HostId,
    doing: string,
    change: (connection: ConnectionLease['connection']) => Promise<T>,
  ): Promise<T> {
    try {
      await using lease = await this.#deps.connection(id);
      return await change(lease.connection);
    } catch (error) {
      throw explained(error, doing, id);
    }
  }

  /** Forget `id`; This Mac stays, back on the app's own checkout. Answers the machines left. */
  async remove(id: HostId): Promise<MachineEntry[]> {
    await this.#deps.removeHost(id);
    return this.machines();
  }
}
