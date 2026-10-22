/**
 * The machines in the main process: which machines are saved (`hosts.json`, through the settings
 * store), what one machine's facts say now (its connection's state, then `machine.describe`), what
 * a path on a machine holds (`checkout.inspect`), and removing one. A machine's status is never
 * stored: it follows from these facts each time they are read. Saving a checkout, signing in and
 * completing paths are the hosts feature's (its `setCheckout` checks a checkout by starting its
 * helper there before it saves it).
 */
import {
  ConnectionError,
  type ConnectionLease,
  HelperRequestError,
  type InspectedCandidate,
  isHostUnsupported,
  type RegistryClusters,
  type RegistrySetup,
  type RunWatch,
} from '@vibesys/connection';
import type {MachineDescription} from '@vibesys/hosts';
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

/** Catalog reads when `runTransportV2` is on. The connection shell owns the implementation. */
export interface RegistryCatalog {
  describeMachine(
    host: HostId,
    expectedCheckout: string | null,
    signal: AbortSignal,
  ): Promise<MachineDescription>;
  listCompute(
    host: HostId,
    expectedCheckout: string | null,
    signal: AbortSignal,
  ): Promise<ComputeSettings>;
}

export interface MachinesMainDeps {
  /** Read the saved hosts; their owner performs all mutations. */
  readonly settings: Pick<SettingsStore, 'hosts'>;
  /** The hosts owner removes one host after fencing its checkout mutations. */
  readonly removeHost: (id: HostId) => Promise<void>;
  /** The checkout `id` runs VibeSys from (This Mac: the app's own unless one is saved). */
  readonly checkout: (id: HostId) => Promise<string | null>;
  /** Lease `id`'s one connection, from its saved checkout. */
  readonly connection: (id: HostId) => Promise<ConnectionLease>;
  /** Per host. Absent or false keeps the helper JSON channel. */
  readonly transportV2?: (id: HostId) => Promise<boolean>;
  /** Required when `transportV2` is true. Missing it throws; it does not fall back. */
  readonly registry?: RegistryCatalog;
  /** Setup reads. Required when `transportV2` is true for inspect. */
  readonly setup?: RegistrySetup;
  /** Cluster changes. Required when `transportV2` is true for put, remove, and test. */
  readonly clusters?: RegistryClusters;
  /** Live runs from WatchRuns. Required when `transportV2` is true for a report. */
  readonly runs?: Pick<RunWatch, 'live'>;
  /** The selection the caller still owns. A change across the await is stale. */
  readonly selectionRevision?: (id: HostId) => bigint;
  /** Host logins when `transportV2` is on. The actor outlives the panel that started one. */
  readonly logins?: {
    start(host: HostId, provider: string): Promise<LoginStart>;
    cancelHost(host: HostId): void;
  };
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

function inspectionOf(inspected: InspectedCandidate): CheckoutInspection {
  switch (inspected.state) {
    case 'found':
      return {
        state: 'found',
        root: inspected.root,
        version: inspected.version,
        commit: inspected.commit,
        dirty: inspected.dirty,
        compatible: inspected.compatible,
      };
    case 'not_found':
      return {state: 'not_found'};
    case 'too_old':
      return {state: 'too_old', version: inspected.version, min: inspected.min};
  }
}

/** A failure the user reads: what was being done on which machine, then the helper's own words. */
function explained(error: unknown, doing: string, id: HostId): Error {
  const detail = error instanceof Error ? error.message : String(error);
  const message = `Could not ${doing} on ${hostLabel(id)}: ${detail}`;
  // A connection failure keeps its typed cause, which the screen words apart from the raw text.
  return error instanceof ConnectionError ? error.reworded(message) : new Error(message);
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
  async report(id: HostId, expectedCheckout: string | null = null): Promise<MachineReport> {
    if (await this.#v2(id)) {
      const registry = this.#registry();
      try {
        const facts = await registry.describeMachine(
          id,
          expectedCheckout,
          new AbortController().signal,
        );
        const live = await this.#runs().live(id);
        return {kind: 'ready', facts: {...facts, runs: {...facts.runs, live}}};
      } catch (error) {
        return unreachable(error);
      }
    }
    // Temporary adapter owned by PR4: the off switch keeps the helper JSON channel.
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
      if (isHostUnsupported(error)) return {kind, facts: null};
      return unreachable(error);
    }
  }

  /** What `path` on `id` holds, by its helper's own inspection. */
  async inspect(
    id: HostId,
    path: string,
    revision: bigint | null = null,
  ): Promise<CheckoutInspection> {
    if (await this.#v2(id)) return this.#inspectCandidate(id, path, revision);
    await using lease = await this.#deps.connection(id);
    try {
      const reply = await lease.connection.request({type: 'checkout.inspect', path});
      return reply.inspection;
    } catch (error) {
      if (isHostUnsupported(error)) return {state: 'unsupported'};
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
    if (await this.#v2(id)) {
      const registry = this.#registry();
      try {
        const value = await registry.listCompute(id, null, new AbortController().signal);
        return {kind: 'offered', value};
      } catch (error) {
        throw explained(error, 'read the clusters and clouds', id);
      }
    }
    // Temporary adapter owned by PR4: the off switch keeps the helper JSON channel.
    return this.#offered(id, 'read the clusters and clouds', async connection => {
      const reply = await connection.request({type: 'compute.list'});
      return reply.settings;
    });
  }

  /** Try `cluster` from `id`: reach it, ask its scheduler, check its workspace. Saves nothing. */
  async testCluster(id: HostId, cluster: SlurmClusterSettings): Promise<ClusterTest> {
    if (await this.#v2(id)) {
      try {
        return await this.#clusters().test(id, cluster, new AbortController().signal);
      } catch (error) {
        throw explained(error, 'test the cluster', id);
      }
    }
    return this.#changed(id, 'test the cluster', async connection => {
      const reply = await connection.request({type: 'compute.test_cluster', cluster});
      return reply.result;
    });
  }

  /** Save `cluster` on `id`; its helper tests it again first. Answers the settings after. */
  async addCluster(id: HostId, cluster: SlurmClusterSettings): Promise<ComputeSettings> {
    if (await this.#v2(id)) {
      try {
        return await this.#clusters().put(id, cluster, new AbortController().signal);
      } catch (error) {
        throw explained(error, 'save the cluster', id);
      }
    }
    return this.#changed(id, 'save the cluster', async connection => {
      const reply = await connection.request({type: 'compute.add_cluster', cluster});
      return reply.settings;
    });
  }

  /** Remove the cluster `name` from `id`'s own file. Answers the settings after. */
  async removeCluster(id: HostId, name: string): Promise<ComputeSettings> {
    if (await this.#v2(id)) {
      try {
        return await this.#clusters().remove(id, name, new AbortController().signal);
      } catch (error) {
        throw explained(error, 'remove the cluster', id);
      }
    }
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
    if (await this.#v2(id)) {
      const logins = this.#deps.logins;
      if (logins === undefined) {
        throw new Error('run transport v2 is on and the login actor is missing');
      }
      return logins.start(id, provider);
    }
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
      if (isHostUnsupported(error)) return {kind: 'unsupported'};
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

  /** `path` on a v2 host, refusing a selection that changed while the host answered. */
  async #inspectCandidate(
    id: HostId,
    path: string,
    revision: bigint | null,
  ): Promise<CheckoutInspection> {
    const current = this.#deps.selectionRevision;
    if (revision !== null && current?.(id) !== revision) throw new Error('stale selection');
    let inspected: InspectedCandidate;
    try {
      inspected = await this.#setup().inspectCandidate(
        id,
        path,
        revision ?? 1n,
        new AbortController().signal,
      );
    } catch (error) {
      if (isHostUnsupported(error)) return {state: 'unsupported'};
      throw error;
    }
    if (revision !== null && current?.(id) !== revision) throw new Error('stale selection');
    return inspectionOf(inspected);
  }

  async #v2(id: HostId): Promise<boolean> {
    return (await this.#deps.transportV2?.(id)) === true;
  }

  /** The setup port. A missing one is a wiring error, not a reason to use the helper. */
  #setup(): RegistrySetup {
    const setup = this.#deps.setup;
    if (setup === undefined) {
      throw new Error('run transport v2 is on and the registry setup is missing');
    }
    return setup;
  }

  /** WatchRuns. A missing port is a wiring error, not a reason to use the helper's count. */
  #runs(): Pick<RunWatch, 'live'> {
    const runs = this.#deps.runs;
    if (runs === undefined) {
      throw new Error('run transport v2 is on and the run watch is missing');
    }
    return runs;
  }

  /** Cluster changes. A missing port is a wiring error, not a reason to use the helper. */
  #clusters(): RegistryClusters {
    const clusters = this.#deps.clusters;
    if (clusters === undefined) {
      throw new Error('run transport v2 is on and the cluster registry is missing');
    }
    return clusters;
  }

  /** The registry port. A missing one is a wiring error, not a reason to use the helper. */
  #registry(): RegistryCatalog {
    const registry = this.#deps.registry;
    if (registry === undefined) {
      throw new Error('run transport v2 is on and the registry reads are missing');
    }
    return registry;
  }

  /** Forget `id`; This Mac stays, back on the app's own checkout. Answers the machines left. */
  async remove(id: HostId): Promise<MachineEntry[]> {
    await this.#deps.removeHost(id);
    return this.machines();
  }
}
