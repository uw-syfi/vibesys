/**
 * The hosts the welcome view offers: which hosts there are, each host's checkout (checked by
 * starting its helper before it is saved), its tasks and directory completions, and its runs as
 * the helper pushes them (`instances.watch`) to the view. Every host action goes through the
 * host's one connection; nothing here retries or polls.
 */
import {readFile} from 'node:fs/promises';
import {
  type ConnectionLease,
  type ConnectionManager,
  connectionStatus,
  HelperRequestError,
  type HostConnection,
} from '@vibesys/connection';
import type {PathsCompleteReply} from '@vibesys/contracts/host-helper';
import {
  type HostId,
  type HostKey,
  type HostListing,
  type HostSummary,
  hostFromKey,
  hostKey,
  hostLabel,
  hostListing,
  inspectionReply,
  validHostPath,
} from '@vibesys/hosts';
import type {PathCompletion, WelcomeInspection} from '@vibesys/ipc';
import {
  rememberInstance,
  type SettingsStore,
  settingsFor,
  sshConfigHosts,
  withHostSettings,
} from '@vibesys/settings';

export interface HostsDeps {
  readonly manager: ConnectionManager;
  /** The settings files' one writer. */
  readonly settings: SettingsStore;
  /** This Mac's checkout when the user has not set one: the checkout the app was built from. */
  readonly localCheckout: string;
  readonly sshConfigPath: string;
  readonly sshConfigFilesystem?: SshConfigFilesystem;
  readonly log: (message: string) => void;
  /** Send a host's listing to the welcome view. */
  readonly push: (listing: HostListing) => void;
  /** Forget a host's retained listing when it leaves the desired set. */
  readonly retire: (host: HostKey) => void;
}

/** The filesystem operation needed to read the user's SSH config. */
export interface SshConfigFilesystem {
  readText(path: string): Promise<string>;
}

const NODE_SSH_CONFIG_FILESYSTEM: SshConfigFilesystem = {
  readText: path => readFile(path, 'utf8'),
};

/** One checkout change, unique across renderer-document lifetimes. */
interface CheckoutChangeId {
  readonly owner: string;
  readonly sequence: number;
}

/** One inspection intent, unique within the renderer document that issued it. */
export interface InspectionChangeId {
  readonly owner: string;
  readonly sequence: number;
}

/** The latest inspection operation for a renderer document and whether it may persist. */
interface InspectionLease {
  readonly sequence: number;
  readonly active: boolean;
}

export class Hosts {
  readonly #deps: HostsDeps;
  readonly #sshConfigFilesystem: SshConfigFilesystem;
  /** Hosts whose registry the view hears, each with the lease that keeps its resources alive. */
  readonly #watched = new Map<HostKey, WatchedHost>();
  /** Checkout validations that still own permission to persist, by renderer-document operation. */
  readonly #checkoutChanges = new Map<string, CheckoutChange>();
  /** Latest inspection lease admitted for each renderer document. */
  readonly #inspectionChanges = new Map<string, InspectionLease>();
  /** Renderer documents whose outstanding inspection writes have been revoked. */
  readonly #retiredInspectionOwners = new Set<string>();
  /** The current panel, owned by the renderer document that selected it. */
  #visible: {readonly owner: string; readonly host: HostKey} | null = null;
  /** Hosts mentioned by the bounded recent-run file. */
  #recent = new Set<HostKey>();
  /** Advances before every desired-set change, fencing async checkout reads. */
  #generation = 0;

  constructor(deps: HostsDeps) {
    this.#deps = deps;
    this.#sshConfigFilesystem = deps.sshConfigFilesystem ?? NODE_SSH_CONFIG_FILESYSTEM;
  }

  /** Why the settings file is ignored, reading it afresh; null when it is fine or absent. */
  async settingsProblem(): Promise<string | null> {
    return (await this.#deps.settings.hosts()).problem;
  }

  /** The concrete aliases in `~/.ssh/config`. */
  async aliases(): Promise<string[]> {
    let text: string;
    try {
      text = await this.#sshConfigFilesystem.readText(this.#deps.sshConfigPath);
    } catch (error) {
      if (isMissingFile(error)) return [];
      throw error;
    }
    return sshConfigHosts(text);
  }

  /** The checkout `id` runs VibeSys from, or null when the user has not set one. */
  async checkout(id: HostId): Promise<string | null> {
    const saved = settingsFor((await this.#deps.settings.hosts()).settings, id)?.checkout ?? null;
    if (saved !== null) return saved;
    return id.kind === 'local' ? this.#deps.localCheckout : null;
  }

  /** Lease the connection to `id`, from its checkout; rejects when none is set. */
  async connection(id: HostId): Promise<ConnectionLease> {
    const checkout = await this.checkout(id);
    if (checkout === null) throw new Error(`Set the VibeSys checkout for ${hostLabel(id)} first.`);
    return this.#deps.manager.acquire(id, checkout);
  }

  /** `id` as its panel shows it; `owner`'s previous panel stops retaining its host. */
  async host(id: HostId, owner: string): Promise<HostSummary> {
    const key = hostKey(id);
    this.#visible = {owner, host: key};
    const generation = this.#changed();
    const checkout = await this.checkout(id);
    await this.#reconcile(generation, new Map([[key, {id, checkout}]]));
    return {key, label: hostLabel(id), checkout};
  }

  /** Reconcile the hosts named by the bounded recent-run file with the current visible host. */
  async recent(hosts: readonly HostId[]): Promise<void> {
    this.#recent = new Set(hosts.map(hostKey));
    await this.#reconcile(this.#changed());
  }

  /** Release the visible intent owned by a renderer document that went away. */
  async retireOwner(owner: string): Promise<void> {
    if (this.#visible?.owner !== owner) return;
    this.#visible = null;
    await this.#reconcile(this.#changed());
  }

  /**
   * Check `path` as `id`'s checkout by starting its helper there, then save it; resolve with the
   * checkout's physical path. The next use of the host runs from it.
   */
  async setCheckout(id: HostId, path: unknown, change: CheckoutChangeId): Promise<string> {
    const checkout = validHostPath(path, `the VibeSys checkout on ${hostLabel(id)}`);
    const key = checkoutChangeKey(change);
    if (this.#checkoutChanges.has(key)) throw new Error('the checkout change is already active');
    const operation = new CheckoutChange(this.#deps.manager.candidate(id, checkout));
    this.#checkoutChanges.set(key, operation);
    try {
      const candidate = await operation.ready();
      const root = candidate.checkoutRoot;
      await operation.closeCandidate();
      operation.assertCurrent();
      const outcome = await this.#deps.settings.updateHosts(
        current => withHostSettings(current, id, {checkout}),
        operation.isCurrent,
      );
      if (outcome === 'cancelled') operation.assertCurrent();
      operation.committed();
      if (this.#desired().has(hostKey(id))) await this.#refresh(id, checkout);
      return root ?? checkout;
    } catch (error) {
      if (operation.cancelled())
        throw new Error('the checkout change was cancelled', {cause: error});
      throw error;
    } finally {
      try {
        await operation.closeCandidate();
      } finally {
        operation.settle();
        if (this.#checkoutChanges.get(key) === operation) this.#checkoutChanges.delete(key);
      }
    }
  }

  /** Revoke one renderer checkout change and stop its candidate connection. */
  async cancelCheckout(change: CheckoutChangeId): Promise<void> {
    const operation = this.#checkoutChanges.get(checkoutChangeKey(change));
    if (operation === undefined) return;
    operation.cancel();
    await operation.closeCandidate();
  }

  /** Revoke and settle every checkout change owned by a renderer document that went away. */
  async cancelCheckoutOwner(owner: string): Promise<void> {
    const operations = [...this.#checkoutChanges.entries()]
      .filter(([key]) => key.startsWith(`${owner}\0`))
      .map(([, operation]) => operation);
    for (const operation of operations) operation.cancel();
    const cleanup = await Promise.allSettled(
      operations.map(operation => operation.closeCandidate()),
    );
    await Promise.all(operations.map(operation => operation.settled));
    const failures = cleanup.flatMap(result =>
      result.status === 'rejected' ? [result.reason] : [],
    );
    if (failures.length > 0)
      throw new AggregateError(failures, 'could not cancel checkout changes');
  }

  /** Sign in to `id` now, prompting when the host asks (the user's own action). */
  async signIn(id: HostId): Promise<void> {
    await this.#deps.manager.signIn(id);
  }

  /** The tasks `project` defines on `id`. */
  async tasks(id: HostId, project: unknown): Promise<string[]> {
    const path = validHostPath(project, 'the project directory');
    await using lease = await this.connection(id);
    try {
      const reply = await lease.connection.request({type: 'tasks.list', project: path});
      return reply.tasks.tasks.map(task => task.name);
    } catch (error) {
      throw plain(error, `Could not read the tasks of ${path}`);
    }
  }

  /** The directories in `directory` (a path ending in `/`, or `~/`) on `id`. */
  async complete(id: HostId, directory: string): Promise<PathCompletion> {
    await using lease = await this.connection(id);
    let reply: PathsCompleteReply;
    try {
      reply = await lease.connection.request({type: 'paths.complete', prefix: directory});
    } catch (error) {
      throw plain(error, `Could not list ${directory}`);
    }
    return {
      directory: reply.directory,
      entries: reply.entries.map(entry => ({
        name: entry.name,
        path: entry.path,
        checkout: entry.vibesys_checkout,
        project: entry.vibesys_project,
      })),
      truncated: reply.truncated,
    };
  }

  /** One coherent observation, persisting its locator only while this renderer intent is current. */
  async inspect(
    id: HostId,
    instanceId: string,
    includeLog: boolean,
    change: InspectionChangeId,
  ): Promise<WelcomeInspection> {
    const current = this.#claimInspection(change);
    await using lease = await this.connection(id);
    let inspection: WelcomeInspection;
    try {
      const reply = await lease.connection.request({
        type: 'instances.inspect',
        instance_id: instanceId,
        include_log: includeLog,
      });
      inspection = inspectionReply(instanceId, reply.inspection);
    } catch (error) {
      if (error instanceof HelperRequestError && error.code === 'feature_not_negotiated') {
        throw new Error(
          `Run inspection is unavailable on ${hostLabel(id)}. Update its VibeSys checkout.`,
        );
      }
      throw plain(error, `Could not inspect ${instanceId}`);
    }
    const identity = inspection.metadata ?? inspection.record;
    if (identity !== null) {
      try {
        await this.#deps.settings.updateRecent(
          recent =>
            rememberInstance(recent, {
              host: hostKey(id),
              project: identity.project_root,
              runId: identity.run_id,
              instanceId,
            }),
          current,
        );
      } catch (error) {
        this.#deps.log(`could not remember the inspected run: ${(error as Error).message}`);
      }
    }
    return inspection;
  }

  /** Revoke every outstanding inspection write owned by a renderer document that went away. */
  retireInspectionOwner(owner: string): void {
    this.#inspectionChanges.delete(owner);
    this.#retiredInspectionOwners.add(owner);
  }

  /** Revoke one pending inspection, without preventing a later inspection from this document. */
  cancelInspection(change: InspectionChangeId): void {
    const lease = this.#inspectionChanges.get(change.owner);
    if (lease?.sequence === change.sequence) {
      this.#inspectionChanges.set(change.owner, {...lease, active: false});
    }
  }

  /** Revoke this document's current inspection when Chrome leaves the welcome surface. */
  cancelInspectionOwner(owner: string): void {
    const lease = this.#inspectionChanges.get(owner);
    if (lease !== undefined) this.#inspectionChanges.set(owner, {...lease, active: false});
  }

  #claimInspection(change: InspectionChangeId): () => boolean {
    if (this.#retiredInspectionOwners.has(change.owner)) {
      throw new Error('the inspection owner was retired');
    }
    const latest = this.#inspectionChanges.get(change.owner);
    if (latest !== undefined && change.sequence <= latest.sequence) {
      throw new Error('the inspection was superseded');
    }
    this.#inspectionChanges.set(change.owner, {sequence: change.sequence, active: true});
    return () => {
      const lease = this.#inspectionChanges.get(change.owner);
      return lease?.sequence === change.sequence && lease.active;
    };
  }

  #install(key: HostKey, lease: ConnectionLease): void {
    const connection = lease.connection;
    // A host returning to the set must overwrite any renderer cache before its old listing could
    // be drawn. A fresh listing, if already known, follows synchronously.
    this.#deps.push({kind: 'unavailable', host: key, connection: {kind: 'connecting'}});
    const push = this.#deps.push;
    const stopListing = connection.watchInstances(listing => push(hostListing(key, listing)));
    const stopPhase = connection.onPhase(phase => {
      if (phase.kind !== 'ready') {
        push({kind: 'unavailable', host: key, connection: connectionStatus(phase)});
      }
    });
    this.#watched.set(key, {
      lease,
      stop: () => {
        stopListing();
        stopPhase();
      },
    });
  }

  #desired(): Set<HostKey> {
    const desired = new Set(this.#recent);
    if (this.#visible !== null) desired.add(this.#visible.host);
    return desired;
  }

  /** Advance intent and synchronously stop forwarding every host that just became obsolete. */
  #changed(): number {
    const generation = ++this.#generation;
    const desired = this.#desired();
    for (const [key, watched] of this.#watched) {
      if (desired.has(key)) continue;
      this.#watched.delete(key);
      watched.stop();
      this.#deps.retire(key);
      void watched.lease[Symbol.asyncDispose]();
    }
    return generation;
  }

  async #reconcile(
    generation: number,
    known = new Map<HostKey, {readonly id: HostId; readonly checkout: string | null}>(),
  ): Promise<void> {
    for (const key of this.#desired()) {
      if (generation !== this.#generation) return;
      if (this.#watched.has(key)) continue;
      await this.#acquireDesired(key, generation, known.get(key));
    }
  }

  async #acquireDesired(
    key: HostKey,
    generation: number,
    supplied: {readonly id: HostId; readonly checkout: string | null} | undefined,
  ): Promise<void> {
    const id = supplied?.id ?? hostFromKey(key);
    const checkout = supplied === undefined ? await this.checkout(id) : supplied.checkout;
    if (!this.#current(key, generation) || checkout === null) return;
    const lease = await this.#deps.manager.acquire(id, checkout);
    if (!this.#current(key, generation) || this.#watched.has(key)) {
      await lease[Symbol.asyncDispose]();
      return;
    }
    try {
      this.#install(key, lease);
    } catch (error) {
      await lease[Symbol.asyncDispose]();
      throw error;
    }
  }

  #current(key: HostKey, generation: number): boolean {
    return generation === this.#generation && this.#desired().has(key);
  }

  async #refresh(id: HostId, checkout: string): Promise<void> {
    const key = hostKey(id);
    const generation = ++this.#generation;
    const lease = await this.#deps.manager.acquire(id, checkout);
    if (generation !== this.#generation || !this.#desired().has(key)) {
      await lease[Symbol.asyncDispose]();
      return;
    }
    const previous = this.#watched.get(key);
    previous?.stop();
    this.#watched.delete(key);
    try {
      this.#install(key, lease);
    } catch (error) {
      await lease[Symbol.asyncDispose]();
      await previous?.lease[Symbol.asyncDispose]();
      throw error;
    }
    await previous?.lease[Symbol.asyncDispose]();
    // Refresh advanced the generation that fences older reads. Fill every other member of the
    // current union so a concurrent recent-host reconciliation cannot leave one unwatched.
    await this.#reconcile(generation);
  }
}

interface WatchedHost {
  readonly lease: ConnectionLease;
  readonly stop: () => void;
}

function isMissingFile(error: unknown): boolean {
  return typeof error === 'object' && error !== null && 'code' in error && error.code === 'ENOENT';
}

/** One checkout validation and the permission it must still own at the settings commit boundary. */
class CheckoutChange {
  readonly settled: Promise<void>;
  readonly #candidate: Promise<HostConnection>;
  #resolveSettled: () => void = () => undefined;
  #state: 'current' | 'cancelled' | 'committed' = 'current';
  #closing: Promise<void> | null = null;

  constructor(candidate: Promise<HostConnection>) {
    this.#candidate = candidate;
    this.settled = new Promise(resolve => {
      this.#resolveSettled = resolve;
    });
  }

  readonly isCurrent = (): boolean => this.#state === 'current';

  cancel(): void {
    if (this.#state === 'current') this.#state = 'cancelled';
  }

  assertCurrent(): void {
    if (this.#state === 'cancelled') throw new Error('the checkout change was cancelled');
  }

  cancelled(): boolean {
    return this.#state === 'cancelled';
  }

  committed(): void {
    this.#state = 'committed';
  }

  settle(): void {
    this.#resolveSettled();
  }

  async ready(): Promise<HostConnection> {
    const candidate = await this.#candidate;
    this.assertCurrent();
    await candidate.ready();
    this.assertCurrent();
    return candidate;
  }

  closeCandidate(): Promise<void> {
    // Acquisition itself may reject when the manager is closing. Cleanup must not replace the
    // primary error from `ready`, but a candidate that was built always remains this operation's.
    this.#closing ??= this.#candidate.then(
      candidate => candidate[Symbol.asyncDispose](),
      () => undefined,
    );
    return this.#closing;
  }
}

function checkoutChangeKey(change: CheckoutChangeId): string {
  return `${change.owner}\0${change.sequence}`;
}

/** A helper's refusal in plain words: what failed, and the helper's own reason. */
function plain(error: unknown, what: string): Error {
  if (error instanceof HelperRequestError) return new Error(`${what}: ${error.message}`);
  return error instanceof Error ? error : new Error(String(error));
}
