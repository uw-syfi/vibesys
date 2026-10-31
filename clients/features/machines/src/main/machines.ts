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
  type InspectedCandidate,
  isHostUnsupported,
  type RegistryClusters,
  type RegistryRunCatalog,
  type RegistrySetup,
  type HostRuns,
} from '@vibesys/connection';
import type {MachineDescription} from '@vibesys/hosts';
import {type HostId, hostFromKey, hostKey, hostLabel} from '@vibesys/hosts';
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

/** Catalog reads when using the gateway. The connection shell owns the implementation. */
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
  /** Required when using the gateway. Missing it throws; it does not fall back. */
  readonly registry?: RegistryCatalog;
  /** Login status reads over the gateway. */
  readonly catalog?: Pick<RegistryRunCatalog, 'logins'>;
  /** Setup reads. Required when using the gateway for inspect. */
  readonly setup?: RegistrySetup;
  /** Cluster changes. Required when using the gateway for put, remove, and test. */
  readonly clusters?: RegistryClusters;
  /** Live runs from WatchRuns. Required when using the gateway for a report. */
  readonly runs?: Pick<HostRuns, 'alive'>;
  /** The selection the caller still owns. A change across the await is stale. */
  readonly selectionRevision?: (id: HostId) => bigint;
  /** Host logins when using the gateway. The actor outlives the panel that started one. */
  readonly logins?: {
    start(host: HostId, provider: string): Promise<LoginStart>;
    cancelHost(host: HostId): void;
  };
}

const LOCAL: HostId = {kind: 'local'};

/**
 * What a failed probe of a machine says about it. A host whose VibeSys lacks what the probe asked
 * (any registry command, or a helper feature) needs an update; it is not unreachable, and
 * Reconnect cannot fix it. Every probe of a machine fails into this one verdict.
 */
function unreachable(error: unknown): MachineReport {
  if (isHostUnsupported(error)) return {kind: 'needs-update', facts: null};
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
    const registry = this.#registry();
    try {
      const facts = await registry.describeMachine(
        id,
        expectedCheckout,
        new AbortController().signal,
      );
      const live = await this.#runs().alive(hostKey(id), new AbortController().signal);
      return {kind: 'ready', facts: {...facts, runs: {...facts.runs, live}}};
    } catch (error) {
      return unreachable(error);
    }
  }

  /** What `path` on `id` holds, by its helper's own inspection. */
  async inspect(
    id: HostId,
    path: string,
    revision: bigint | null = null,
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

  /**
   * What `id` has set up as places to measure (its Slurm clusters and Modal token), or that its
   * helper predates machine compute settings.
   */
  async compute(id: HostId): Promise<Offered<ComputeSettings>> {
    const registry = this.#registry();
    try {
      const value = await registry.listCompute(id, null, new AbortController().signal);
      return {kind: 'offered', value};
    } catch (error) {
      if (isHostUnsupported(error)) return {kind: 'unsupported'};
      throw explained(error, 'read the clusters and clouds', id);
    }
  }

  /** Try `cluster` from `id`: reach it, ask its scheduler, check its workspace. Saves nothing. */
  async testCluster(id: HostId, cluster: SlurmClusterSettings): Promise<ClusterTest> {
    try {
      return await this.#clusters().test(id, cluster, new AbortController().signal);
    } catch (error) {
      throw explained(error, 'test the cluster', id);
    }
  }

  /** Save `cluster` on `id`; its helper tests it again first. Answers the settings after. */
  async addCluster(id: HostId, cluster: SlurmClusterSettings): Promise<ComputeSettings> {
    try {
      return await this.#clusters().put(id, cluster, new AbortController().signal);
    } catch (error) {
      throw explained(error, 'save the cluster', id);
    }
  }

  /** Remove the cluster `name` from `id`'s own file. Answers the settings after. */
  async removeCluster(id: HostId, name: string): Promise<ComputeSettings> {
    try {
      return await this.#clusters().remove(id, name, new AbortController().signal);
    } catch (error) {
      throw explained(error, 'remove the cluster', id);
    }
  }

  /** Each vetted harness's login on `id`, or that its helper predates login state. */
  async agentLogins(id: HostId): Promise<Offered<readonly AgentLogin[]>> {
    const catalog = this.#deps.catalog;
    if (catalog === undefined) throw new Error('the login catalog is missing');
    try {
      return {kind: 'offered', value: await catalog.logins(id, new AbortController().signal)};
    } catch (error) {
      throw explained(error, 'read the agent logins', id);
    }
  }

  /** Start `provider`'s own login on `id`. */
  async startLogin(id: HostId, provider: string): Promise<LoginStart> {
    const logins = this.#deps.logins;
    if (logins === undefined) {
      throw new Error('the login actor is missing');
    }
    return logins.start(id, provider);
  }

  /** The setup port. A missing one is a wiring error, not a reason to use the helper. */
  #setup(): RegistrySetup {
    const setup = this.#deps.setup;
    if (setup === undefined) {
      throw new Error('the registry setup is missing');
    }
    return setup;
  }

  /** The runs subscription. A missing port is a wiring error, not a reason to use the helper's count. */
  #runs(): Pick<HostRuns, 'alive'> {
    const runs = this.#deps.runs;
    if (runs === undefined) {
      throw new Error('the host runs are missing');
    }
    return runs;
  }

  /** Cluster changes. A missing port is a wiring error, not a reason to use the helper. */
  #clusters(): RegistryClusters {
    const clusters = this.#deps.clusters;
    if (clusters === undefined) {
      throw new Error('the cluster registry is missing');
    }
    return clusters;
  }

  /** The registry port. A missing one is a wiring error, not a reason to use the helper. */
  #registry(): RegistryCatalog {
    const registry = this.#deps.registry;
    if (registry === undefined) {
      throw new Error('the registry reads are missing');
    }
    return registry;
  }

  /** Forget `id`; This Mac stays, back on the app's own checkout. Answers the machines left. */
  async remove(id: HostId): Promise<MachineEntry[]> {
    await this.#deps.removeHost(id);
    return this.machines();
  }
}
