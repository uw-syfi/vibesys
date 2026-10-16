/**
 * The machines in the main process: which machines are saved (`hosts.json`, through the settings
 * store), what one machine's facts say now (its connection's state, then `machine.describe`), what
 * a path on a machine holds (`checkout.inspect`), and removing one. A machine's status is never
 * stored: it follows from these facts each time they are read. Saving a checkout, signing in and
 * completing paths are the hosts feature's (its `setCheckout` checks a checkout by starting its
 * helper there before it saves it).
 */
import {ConnectionError, type ConnectionLease, HelperRequestError} from '@vibesys/connection';
import {type HostId, hostFromKey, hostKey, hostLabel} from '@vibesys/hosts';
import type {CheckoutInspection, MachineEntry, MachineReport} from '@vibesys/ipc';
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
    try {
      const reply = await held.connection.request({type: 'machine.describe'});
      return {kind: 'ready', facts: reply.machine};
    } catch (error) {
      if (notNegotiated(error)) return {kind: 'ready', facts: null};
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
        ? new Error(`Could not inspect ${path}: ${error.message}`)
        : error;
    }
  }

  /** Forget `id`; This Mac stays, back on the app's own checkout. Answers the machines left. */
  async remove(id: HostId): Promise<MachineEntry[]> {
    await this.#deps.removeHost(id);
    return this.machines();
  }
}
