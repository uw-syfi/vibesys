/**
 * The hosts the welcome view offers: which hosts there are, each host's checkout (checked by
 * starting its helper before it is saved), its tasks and directory completions, and its runs as
 * the helper pushes them (`instances.watch`) to the view, and the inspector's run logs. Every host
 * action goes through the host's one connection; nothing here retries or polls.
 */
import {
  type ConnectionLease,
  type ConnectionManager,
  HelperRequestError,
  hostListingFromRuns,
  isHostUnsupported,
  type LogsLease,
  type RegistryRunCatalog,
  type RegistryRunReads,
  type RegistrySavedRuns,
  type RegistrySetup,
  type RunWatch,
} from '@vibesys/connection';
import {
  type HarnessesListReply,
  type HelperMessage,
  RUN_EVENT_TYPES_BY_KINDS,
  type RunEventKinds,
  type UsageRecord,
} from '@vibesys/contracts/host-helper';
import {
  type HostId,
  type HostKey,
  type HostListing,
  type HostSummary,
  hostFromKey,
  hostKey,
  hostLabel,
  type InstanceInspection,
  inspectionReply,
  type ListedRun,
  type RunDigestsReply,
  type UsageWindowOutcome,
  usageWindowRefusal,
  validHostPath,
} from '@vibesys/hosts';
import type {
  HarnessListing,
  HarnessOffer,
  PathCompletion,
  RunHistoryPage,
  RunLogsRequest,
  RunSpanPage,
  RunSummary,
  RunSummaryResult,
  SummaryTokens,
} from '@vibesys/ipc';
import type {LogListOutcome, LogReadOutcome} from '@vibesys/logs';
import {
  configuredMachines,
  rememberInstance,
  type SettingsStore,
  settingsFor,
  withHostSettings,
  withoutHost,
} from '@vibesys/settings';
import {checkHostVersion, type HostVersion, HostVersionGate} from './host-version.js';
import {discoverSshConfigAliases, type SshConfigFilesystem} from './ssh-config.js';

/** The most events a helper without `run_history_pages` serves in one `runs.read_events` page. */
const LEGACY_HISTORY_PAGE_EVENTS = 500;

/** The events of `events` whose `type` `kinds` selects (`RUN_EVENT_TYPES_BY_KINDS`), in their order. */
function ofKinds(events: readonly unknown[], kinds: RunEventKinds): unknown[] {
  const types = new Set(RUN_EVENT_TYPES_BY_KINDS[kinds]);
  return events.filter(
    event =>
      typeof event === 'object' &&
      event !== null &&
      'type' in event &&
      typeof event.type === 'string' &&
      types.has(event.type),
  );
}

export interface HostsDeps {
  readonly manager: ConnectionManager;
  /** The settings files' one writer. */
  readonly settings: SettingsStore;
  /** This Mac's checkout when the user has not set one: the checkout the app was built from. */
  readonly localCheckout: string;
  readonly sshConfigPath: string;
  readonly sshConfigFilesystem?: SshConfigFilesystem;
  readonly sshHomePath?: string;
  readonly log: (message: string) => void;
  /** Send a host's listing to the welcome view. */
  readonly push: (listing: HostListing) => void;
  /** Forget a host's retained listing when it leaves the desired set. */
  readonly retire: (host: HostKey) => void;
  /** Required when using the gateway. Missing it throws; it does not fall back. */
  readonly setup?: RegistrySetup;
  /** Stop a host login when its checkout is replaced or the host is removed. */
  readonly endLogin?: (id: HostId) => void;
  /** The host's runs, from its gateway's WatchRuns while the host is shown. Cancel reaps the stream. */
  readonly runs: Pick<RunWatch, 'observe' | 'cancel' | 'retry'>;
  readonly savedRuns?: RegistrySavedRuns;
  /** Digests and the usage window, read through the gateway. */
  readonly runReads?: RegistryRunReads;
  readonly catalog?: RegistryRunCatalog;
}

/** How many times a summary is read again when its pages disagree on where the run stood. */
const SUMMARY_READ_ATTEMPTS = 3;

type SummaryReply = Extract<HelperMessage, {type: 'runs.summary'}>;

const tokens = (tally: SummaryReply['tokens']): SummaryTokens => ({
  input: tally.input,
  cacheRead: tally.cache_read,
  cacheWrite: tally.cache_write,
  output: tally.output,
  reasoning: tally.reasoning,
});

/**
 * Every page of one run's summary joined, or null when the pages disagree on the sequence the run
 * stood at (it wrote between two of them): the caller reads again.
 */
async function readSummaryPages(
  connection: {
    request(body: {type: 'runs.summary'} & Record<string, unknown>): Promise<SummaryReply>;
  },
  project: string,
  runId: string,
  withEnding: boolean,
): Promise<RunSummary | null> {
  const spans: unknown[] = [];
  const replies: SummaryReply['replies'][number][] = [];
  const usage: SummaryReply['usage'][number][] = [];
  const records: UsageRecord[] = [];
  const schemas = new Map<string, unknown>();
  let first: SummaryReply | null = null;
  let offset: number | null = 0;
  while (offset !== null) {
    const page: SummaryReply = await connection.request({
      type: 'runs.summary',
      project,
      run_id: runId,
      offset,
      // A helper without `run_ending` is not asked: its summary has no gates or terminal event.
      ...(withEnding ? {with_ending: true} : {}),
    });
    if (first !== null && page.through_sequence !== first.through_sequence) return null;
    first ??= page;
    spans.push(...page.spans);
    replies.push(...page.replies);
    usage.push(...page.usage);
    records.push(...page.records);
    for (const [key, schema] of Object.entries(page.schemas)) schemas.set(key, schema);
    offset = page.next_offset ?? null;
  }
  if (first === null) return null;
  return {
    throughSequence: first.through_sequence,
    eventCount: first.event_count,
    journalBytes: first.journal_bytes,
    trace: first.trace,
    roundsFinished: first.rounds_finished,
    overview: first.overview,
    spans,
    replies: replies.map(reply => ({
      spanId: reply.span_id,
      kind: reply.kind,
      jsonSchema: schemas.get(reply.schema_id) ?? null,
      value: reply.value,
    })),
    tokens: tokens(first.tokens),
    usage: usage.map(entry => ({spanId: entry.span_id, tokens: tokens(entry.tally)})),
    records,
    inputGates: (first.input_gates ?? []).map(gate => ({
      sequence: gate.sequence,
      gate: gate.gate,
      recipe: gate.recipe,
      passed: gate.passed,
      outputTail: gate.output_tail,
    })),
    terminal: first.terminal ?? null,
  };
}

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

/** How a pair reads, as the app's own type: only the fields the helper set. */
function harnessDisplay(
  display: NonNullable<HarnessesListReply['harnesses'][number]['display']>,
): NonNullable<HarnessOffer['display']> {
  return {
    providerName: display.provider_name,
    ...(display.model_name == null ? {} : {modelName: display.model_name}),
    ...(display.family == null ? {} : {family: display.family}),
    ...(display.tier == null ? {} : {tier: display.tier}),
    ...(display.tier_name == null ? {} : {tierName: display.tier_name}),
    ...(display.maker == null ? {} : {maker: display.maker}),
    ...(display.generation == null ? {} : {generation: display.generation}),
  };
}

export class Hosts {
  readonly #deps: HostsDeps;
  /** Hosts whose runs the view hears, each with the stop that releases its stream. */
  readonly #watched = new Map<HostKey, WatchedHost>();
  /** Checkout validations that still own permission to persist, by renderer-document operation. */
  readonly #checkoutChanges = new Map<string, CheckoutChange>();
  /** Hosts whose removal owns the mutation boundary; no new checkout save may enter. */
  readonly #removing = new Set<HostKey>();
  /** The latest committed checkout identity for each host, fencing checkout-derived leases. */
  readonly #checkoutRevisions = new Map<HostKey, CheckoutRevision>();
  /** The logs channel each renderer document's inspector holds, on one host at a time. */
  readonly #logs = new Map<string, InspectorLogs>();
  /** Latest inspection lease admitted for each renderer document. */
  readonly #inspectionChanges = new Map<string, InspectionLease>();
  /** Renderer documents whose outstanding inspection writes have been revoked. */
  readonly #retiredInspectionOwners = new Set<string>();
  /** The current panel, owned by the renderer document that selected it. */
  #visible: {readonly owner: string; readonly host: HostKey} | null = null;
  /** Hosts mentioned by the bounded recent-run file. */
  #recent = new Set<HostKey>();
  /** Hosts the user configured (`hosts.json`): watched even when no recent run names them. */
  #configured = new Set<HostKey>();
  /** The one replacement currently allowed to install for each host. */
  readonly #watchIntents = new Map<HostKey, WatchIntent>();
  /** Advances before desired-set changes, fencing reads against the desired-host union. */
  #generation = 0;

  constructor(deps: HostsDeps) {
    this.#deps = deps;
  }

  /** Why the settings file is ignored, reading it afresh; null when it is fine or absent. */
  async settingsProblem(): Promise<string | null> {
    return (await this.#deps.settings.hosts()).problem;
  }

  /** The concrete aliases in `~/.ssh/config`. */
  async aliases(): Promise<string[]> {
    return discoverSshConfigAliases(this.#deps.sshConfigPath, {
      filesystem: this.#deps.sshConfigFilesystem,
      homePath: this.#deps.sshHomePath,
    });
  }

  /** The checkout `id` runs VibeSys from, or null when the user has not set one. */
  async checkout(id: HostId): Promise<string | null> {
    const saved = settingsFor((await this.#deps.settings.hosts()).settings, id)?.checkout ?? null;
    if (saved !== null) return saved;
    return id.kind === 'local' ? this.#deps.localCheckout : null;
  }

  /** Lease the connection to `id`, from its checkout; rejects when none is set. */
  async connection(id: HostId): Promise<ConnectionLease> {
    const key = hostKey(id);
    for (;;) {
      const revision = this.#checkoutRevisions.get(key);
      const checkout = await this.checkout(id);
      if (this.#checkoutRevisions.get(key) !== revision) continue;
      if (checkout === null) {
        throw new Error(`Set the VibeSys checkout for ${hostLabel(id)} first.`);
      }
      let lease: ConnectionLease;
      try {
        lease = await this.#deps.manager.acquire(id, checkout);
      } catch (error) {
        if (this.#checkoutRevisions.get(key) !== revision) {
          await this.#repairDesired(key);
          continue;
        }
        throw error;
      }
      if (this.#checkoutRevisions.get(key) === revision) return lease;
      // The old checkout may already have displaced the manager's shown slot. Releasing its exact
      // lease before retrying restores convergence without exposing that stale connection.
      await lease[Symbol.asyncDispose]();
      await this.#repairDesired(key);
    }
  }

  /** `id` as its panel shows it; `owner`'s previous panel stops retaining its host. */
  async host(id: HostId, owner: string): Promise<HostSummary> {
    const key = hostKey(id);
    this.#visible = {owner, host: key};
    const generation = this.#changed();
    const revision = this.#checkoutRevisions.get(key);
    const checkout = await this.checkout(id);
    await this.#reconcile(generation, new Map([[key, {id, checkout, revision}]]));
    return {key, label: hostLabel(id), checkout};
  }

  /** Reconcile the hosts named by the bounded recent-run file with the current visible host. */
  async recent(hosts: readonly HostId[]): Promise<void> {
    this.#recent = new Set(hosts.map(hostKey));
    await this.#reconcile(this.#changed());
  }

  /**
   * Reconcile the hosts of `hosts.json` with the current ones: every configured machine is watched,
   * so its live and recent runs list on a profile with no recent file.
   */
  async configured(): Promise<void> {
    const {settings} = await this.#deps.settings.hosts();
    this.#configured = new Set(configuredMachines(settings).map(hostKey));
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
    const host = hostKey(id);
    if (this.#removing.has(host)) throw new Error(`${hostLabel(id)} is being removed`);
    const key = checkoutChangeKey(change);
    if (this.#checkoutChanges.has(key)) throw new Error('the checkout change is already active');
    const operation = new CheckoutChange(host);
    this.#checkoutChanges.set(key, operation);
    try {
      const root = await this.#inspectForCheckout(id, checkout, change.sequence, operation);
      const outcome = await this.#deps.settings.updateHosts(
        current => withHostSettings(current, id, {...settingsFor(current, id), checkout}),
        operation.isCurrent,
      );
      if (outcome === 'cancelled') operation.assertCurrent();
      operation.committed();
      this.#deps.endLogin?.(id);
      const revision = this.#commitCheckout(hostKey(id), checkout);
      if (this.#desired().has(hostKey(id))) await this.#refresh(id, revision);
      return root ?? checkout;
    } catch (error) {
      if (operation.cancelled())
        throw new Error('the checkout change was cancelled', {cause: error});
      throw error;
    } finally {
      operation.settle();
      if (this.#checkoutChanges.get(key) === operation) this.#checkoutChanges.delete(key);
    }
  }

  /** Revoke one renderer checkout change and stop its candidate connection. */
  cancelCheckout(change: CheckoutChangeId): Promise<void> {
    const operation = this.#checkoutChanges.get(checkoutChangeKey(change));
    if (operation === undefined) return Promise.resolve();
    operation.cancel();
    return Promise.resolve();
  }

  /** Revoke and settle every checkout change owned by a renderer document that went away. */
  async cancelCheckoutOwner(owner: string): Promise<void> {
    const operations = [...this.#checkoutChanges.entries()]
      .filter(([key]) => key.startsWith(`${owner}\0`))
      .map(([, operation]) => operation);
    for (const operation of operations) operation.cancel();
    await Promise.all(operations.map(operation => operation.settled));
  }

  /** Remove `id` after revoking every older checkout save that could recreate it. */
  async remove(id: HostId): Promise<void> {
    const key = hostKey(id);
    if (this.#removing.has(key)) throw new Error(`${hostLabel(id)} is already being removed`);
    this.#removing.add(key);
    this.#deps.endLogin?.(id);
    try {
      const operations = [...this.#checkoutChanges.values()].filter(
        operation => operation.host === key && operation.cancel(),
      );
      await Promise.all(operations.map(operation => operation.settled));
      let persistenceFailure: {readonly error: unknown} | null = null;
      try {
        await this.#deps.settings.updateHosts(current => withoutHost(current, id));
      } catch (error) {
        // Atomic replacement may already be visible when its directory sync reports failure.
        // Reconcile from the authoritative file on both sides of that ambiguous boundary before
        // releasing the mutation fence, while preserving the durability failure for the caller.
        persistenceFailure = {error};
      }
      this.#checkoutRevisions.set(key, {removed: true});
      this.#watchIntents.delete(key);
      this.#changed();
      await this.#repairDesired(key);
      if (persistenceFailure !== null) throw persistenceFailure.error;
    } finally {
      this.#removing.delete(key);
    }
  }

  /** Sign in to `id` now, prompting when the host asks (the user's own action). */
  async signIn(id: HostId): Promise<void> {
    await this.#deps.manager.signIn(id);
    this.#watched.get(hostKey(id))?.recheck();
  }

  /** Ask `id` for its runs again now, the user's retry of a machine that stopped. */
  retryListing(id: HostId): void {
    this.#watched.get(hostKey(id))?.retry();
  }

  /** The tasks `project` defines on `id`. */
  async tasks(id: HostId, project: unknown): Promise<string[]> {
    const path = validHostPath(project, 'the project directory');
    try {
      return await this.#catalog().tasks(id, path, new AbortController().signal);
    } catch (error) {
      throw plain(error, 'Could not read the tasks of the project');
    }
  }

  /**
   * One page of run `runId`'s recorded events in `project` on `id` (`runs.read_events`), from
   * `from`; `unsupported` when the host's VibeSys does not serve history (`run_history`).
   *
   * With `kinds` the page holds only the events of that kind, whichever helper serves it: a helper
   * with `run_trace_events` selects them from the journal's index and sends only those; an older
   * one sends every event of the page and they are selected here. Either way the page's `next` and
   * `complete` are the helper's, so the pages of a read join to the same events. The totals of a
   * page that an older helper selects here are the journal's, not the selection's.
   */
  async readEvents(
    id: HostId,
    project: string,
    runId: string,
    from: number,
    max: number,
    kinds?: RunEventKinds,
  ): Promise<RunHistoryPage> {
    await using lease = await this.connection(id);
    try {
      // A helper without `run_history_pages` refuses more events per page than it served before
      // the feature, and does not name the journal's totals.
      await lease.connection.ready();
      const pages = lease.connection.features?.includes('run_history_pages') === true;
      const selects = lease.connection.features?.includes('run_trace_events') === true;
      const reply = await lease.connection.request({
        type: 'runs.read_events',
        project,
        run_id: runId,
        from_sequence: from,
        max: pages ? max : Math.min(max, LEGACY_HISTORY_PAGE_EVENTS),
        ...(pages ? {with_totals: true} : {}),
        ...(kinds !== undefined && selects ? {kinds} : {}),
      });
      return {
        kind: 'page',
        events: kinds !== undefined && !selects ? ofKinds(reply.events, kinds) : reply.events,
        next: reply.next_sequence,
        complete: reply.complete,
        ...(typeof reply.total_events === 'number' ? {totalEvents: reply.total_events} : {}),
        ...(typeof reply.total_bytes === 'number' ? {totalBytes: reply.total_bytes} : {}),
      };
    } catch (error) {
      if (isHostUnsupported(error)) {
        return {kind: 'unsupported'};
      }
      throw plain(error, `Could not read the history of run ${runId}`);
    }
  }

  /** One bounded page of an opened span's recorded events; older helpers may not serve it. */
  async readSpan(
    id: HostId,
    project: string,
    runId: string,
    spanId: string,
    from: number,
    max: number,
  ): Promise<RunSpanPage> {
    await using lease = await this.connection(id);
    try {
      await lease.connection.ready();
      if (lease.connection.features?.includes('run_span_events') !== true) {
        return {kind: 'unsupported'};
      }
      const reply = await lease.connection.request({
        type: 'runs.events_for_span',
        project,
        run_id: runId,
        span_id: spanId,
        from_sequence: from,
        max,
      });
      return {
        kind: 'page',
        events: reply.events,
        next: reply.next_sequence,
        complete: reply.complete,
        total: reply.total,
      };
    } catch (error) {
      if (isHostUnsupported(error)) {
        return {kind: 'unsupported'};
      }
      throw plain(error, `Could not read span ${spanId} of run ${runId}`);
    }
  }

  /**
   * Run `runId`'s summary in `project` on `id` (`runs.summary`): what the run level shows, without
   * the history it comes from, its pages joined. A run still writing can move between pages; every
   * page names the sequence it was read at, and a read whose pages disagree starts again, a few
   * times before it fails. `unsupported` when the host's VibeSys does not serve summaries, `gone` when it holds no
   * record of the run.
   */
  async readSummary(id: HostId, project: string, runId: string): Promise<RunSummaryResult> {
    await using lease = await this.connection(id);
    try {
      // A helper without the features `runs.summary` is gated by (`run_summary`, `run_trace`) is
      // refused by the channel before it is asked: `unsupported` below.
      await lease.connection.ready();
      const withEnding = lease.connection.features?.includes('run_ending') === true;
      for (let attempt = 0; attempt < SUMMARY_READ_ATTEMPTS; attempt += 1) {
        const summary = await readSummaryPages(lease.connection, project, runId, withEnding);
        if (summary !== null) return {kind: 'summary', summary};
      }
      throw new Error('the run kept changing while its summary was read');
    } catch (error) {
      if (isHostUnsupported(error)) {
        return {kind: 'unsupported'};
      }
      if (error instanceof HelperRequestError && error.code === 'unknown_run') {
        return {kind: 'gone'};
      }
      throw plain(error, `Could not read the summary of run ${runId}`);
    }
  }

  /**
   * What each of `runIds` in `project` on `id` says at a glance, with the tiles' data, as the host's
   * registered read states it; `unsupported` when the host's VibeSys does not offer the read.
   */
  async runDigests(
    id: HostId,
    project: string,
    runIds: readonly string[],
  ): Promise<RunDigestsReply> {
    try {
      return await this.#runReads().digests(id, project, runIds, new AbortController().signal);
    } catch (error) {
      if (isHostUnsupported(error)) return {kind: 'unsupported'};
      throw plain(error, 'Could not read the runs of the project');
    }
  }

  /**
   * What `runIds` of `project` on `id` spent in `[since, until)`, both in milliseconds;
   * `unsupported` when the host's VibeSys does not offer it. `until` is "now" for the rate: the
   * host reads no clock. A window or list the host would refuse (see `usageWindowRefusal`) is a
   * mistake of the caller and throws before any request.
   */
  async usageWindow(
    id: HostId,
    project: string,
    runIds: readonly string[],
    since: number,
    until: number,
  ): Promise<UsageWindowOutcome> {
    const refusal = usageWindowRefusal(runIds, since, until);
    if (refusal !== null) throw new Error(refusal);
    try {
      return await this.#runReads().usageWindow(
        id,
        project,
        runIds,
        since,
        until,
        new AbortController().signal,
      );
    } catch (error) {
      if (isHostUnsupported(error)) return {kind: 'unsupported'};
      throw plain(error, 'Could not read what the runs of the project spent');
    }
  }

  /** Every run of `project` on `id`, newest first (`runs.list`); a digest is asked for separately. */
  async projectRuns(id: HostId, project: string): Promise<readonly ListedRun[]> {
    if (!this.#deps.savedRuns) throw new Error('saved-run registry is missing');
    return this.#deps.savedRuns.list(id, project, new AbortController().signal);
  }

  /** The vetted provider and model pairs runs on `id` may start with; `unsupported` on an older helper. */
  async harnesses(id: HostId): Promise<HarnessListing> {
    try {
      const harnesses = await this.#catalog().harnesses(id, new AbortController().signal);
      return {
        kind: 'listed',
        harnesses: harnesses.map(({provider, model, default: isDefault, display}) => ({
          provider,
          model,
          default: isDefault,
          ...(display == null ? {} : {display: harnessDisplay(display)}),
        })),
      };
    } catch (error) {
      if (isHostUnsupported(error)) {
        return {kind: 'unsupported'};
      }
      throw plain(error, `Could not read the vetted models of ${hostLabel(id)}`);
    }
  }

  /** The directories in `directory` (a path ending in `/`, or `~/`) on `id`. */
  async complete(id: HostId, directory: string): Promise<PathCompletion> {
    try {
      const listed = await this.#setup().completePaths(id, directory, new AbortController().signal);
      return {
        kind: 'listed',
        directory: listed.directory,
        entries: listed.entries,
        truncated: listed.truncated,
      };
    } catch (error) {
      if (isHostUnsupported(error)) return {kind: 'unsupported'};
      throw error;
    }
  }

  /** One coherent observation, persisting its locator only while this renderer intent is current. */
  async inspect(
    id: HostId,
    instanceId: string,
    change: InspectionChangeId,
  ): Promise<InstanceInspection> {
    const current = this.#claimInspection(change);
    await using lease = await this.connection(id);
    let inspection: InstanceInspection;
    try {
      const reply = await lease.connection.request({
        type: 'instances.inspect',
        instance_id: instanceId,
      });
      inspection = inspectionReply(instanceId, reply.inspection);
    } catch (error) {
      if (isHostUnsupported(error)) {
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

  /**
   * Answer one logs request from `owner`'s inspector about `instanceId` on `id`, over the host's
   * logs channel (`docs/contributing/desktop-connection.md`, "Logs"). The first request takes a
   * connection lease and a logs lease, which `owner` keeps while it asks about that host; asking
   * about another host, `releaseLogs`, or the owner going away lets them go, aborting the reads
   * still in flight. The parser already held a read to `instanceId`. A `withdraw` aborts `owner`'s
   * read in flight with that token, which withdraws the helper's long poll; a token naming no read
   * in flight (it already answered) is a no-op.
   */
  async logs(
    id: HostId,
    instanceId: string,
    request: RunLogsRequest,
    owner: string,
  ): Promise<LogListOutcome | LogReadOutcome | null> {
    if (request.kind === 'withdraw') {
      this.#logs.get(owner)?.withdraw(request.token);
      return null;
    }
    if (this.#retiredInspectionOwners.has(owner)) {
      throw new Error('the inspection owner was retired');
    }
    const key = hostKey(id);
    let held = this.#logs.get(owner);
    if (held?.host !== key) {
      if (held !== undefined) this.releaseLogs(owner);
      held = new InspectorLogs(key, this.connection(id));
      this.#logs.set(owner, held);
    }
    let logs: LogsLease;
    try {
      logs = await held.lease();
    } catch (error) {
      // A hold that could not connect is not kept: the next request tries again.
      if (this.#logs.get(owner) === held) this.#logs.delete(owner);
      throw error;
    }
    if (request.kind === 'list') return logs.list(instanceId);
    return held.read(request.token, signal => logs.read(request.read, signal));
  }

  /** Let `owner`'s logs channel go; the channel closes once no other holder uses it. */
  releaseLogs(owner: string): void {
    const held = this.#logs.get(owner);
    if (held === undefined) return;
    this.#logs.delete(owner);
    void held
      .release()
      .catch(error =>
        this.#deps.log(
          `could not release the logs of ${hostLabel(hostFromKey(held.host))}: ${
            error instanceof Error ? error.message : String(error)
          }`,
        ),
      );
  }

  /** Revoke every outstanding inspection write owned by a renderer document that went away. */
  retireInspectionOwner(owner: string): void {
    this.#inspectionChanges.delete(owner);
    this.#retiredInspectionOwners.add(owner);
    this.releaseLogs(owner);
  }

  /** Revoke one pending inspection, without preventing a later inspection from this document. */
  cancelInspection(change: InspectionChangeId): void {
    const lease = this.#inspectionChanges.get(change.owner);
    if (lease?.sequence === change.sequence) {
      this.#inspectionChanges.set(change.owner, {...lease, active: false});
    }
  }

  /** Revoke this document's current inspection, and its logs, when Chrome leaves the welcome surface. */
  cancelInspectionOwner(owner: string): void {
    const lease = this.#inspectionChanges.get(owner);
    if (lease !== undefined) this.#inspectionChanges.set(owner, {...lease, active: false});
    this.releaseLogs(owner);
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

  /**
   * Show `key`'s runs as its gateway's WatchRuns states them. The listing and the connection status
   * are both that stream's: the host's helper is not asked, so a helper that cannot start does not
   * hide a gateway that can list.
   */
  #install(key: HostKey, checkout: string): void {
    const push = this.#deps.push;
    let stopped = false;
    const gate = new HostVersionGate({
      host: key,
      check: signal => this.#checkVersion(key, checkout, signal),
      push,
      log: reason =>
        this.#deps.log(`could not check the VibeSys of ${hostLabel(hostFromKey(key))}: ${reason}`),
    });
    let live = false;
    const stopRuns = this.#deps.runs.observe(hostFromKey(key), state => {
      if (stopped) return;
      const listing = hostListingFromRuns(key, state);
      if (listing.kind === 'listed') {
        gate.listing(listing);
        if (!live) gate.recheck();
      } else {
        gate.invalidate();
        push(listing);
      }
      live = listing.kind === 'listed';
    });
    this.#watched.set(key, {
      checkout,
      recheck: () => {
        if (live) gate.recheck();
      },
      retry: () => {
        if (live) gate.recheck();
        else this.#deps.runs.retry(hostFromKey(key));
      },
      stop: () => {
        stopped = true;
        gate.invalidate();
        stopRuns();
        this.#deps.runs.cancel(hostFromKey(key));
      },
    });
  }

  /** Whether a host's checkout can serve this app. */
  async #checkVersion(key: HostKey, checkout: string, signal: AbortSignal): Promise<HostVersion> {
    const id = hostFromKey(key);
    return checkHostVersion(this.#setup(), id, checkout, signal);
  }

  #desired(): Set<HostKey> {
    const desired = new Set(this.#recent);
    for (const key of this.#configured) {
      // A removal revokes the host before any stale read of `hosts.json` could name it again.
      if (!('removed' in (this.#checkoutRevisions.get(key) ?? {}))) desired.add(key);
    }
    if (this.#visible !== null) desired.add(this.#visible.host);
    return desired;
  }

  /** Advance intent and synchronously stop forwarding every host that just became obsolete. */
  #changed(): number {
    const generation = ++this.#generation;
    const desired = this.#desired();
    // A host that actually left the desired set revokes an acquisition even if it is re-added
    // before that acquisition returns. Updates which keep it desired leave its per-host intent
    // alone, so they cannot supersede a committed checkout replacement.
    for (const key of this.#watchIntents.keys()) {
      if (!desired.has(key)) this.#watchIntents.delete(key);
    }
    for (const [key, watched] of this.#watched) {
      if (desired.has(key)) continue;
      this.#watched.delete(key);
      this.#retireWatched(key, watched);
    }
    return generation;
  }

  /** Attempt every retirement step even when presentation cleanup itself reports a failure. */
  #retireWatched(key: HostKey, watched: WatchedHost): void {
    try {
      watched.stop();
    } catch (error) {
      this.#logRetirementFailure(key, error);
    }
    try {
      this.#deps.retire(key);
    } catch (error) {
      this.#logRetirementFailure(key, error);
    }
  }

  #logRetirementFailure(key: HostKey, error: unknown): void {
    try {
      this.#deps.log(
        `could not retire ${hostLabel(hostFromKey(key))} connection: ${
          error instanceof Error ? error.message : String(error)
        }`,
      );
    } catch {
      // Diagnostics cannot become a second unhandled failure after the manager retained its barrier.
    }
  }

  async #reconcile(
    generation: number,
    known = new Map<
      HostKey,
      {
        readonly id: HostId;
        readonly checkout: string | null;
        readonly revision: CheckoutRevision | undefined;
      }
    >(),
  ): Promise<void> {
    for (const key of this.#desired()) {
      if (generation !== this.#generation) return;
      try {
        await this.#acquireDesired(key, generation, known.get(key));
      } catch (error) {
        // One host's acquisition failing (routinely: the manager disposing mid-shutdown) must
        // not stop this pass from reaching every other desired host.
        this.#logConnectionFailure(key, error);
      }
    }
  }

  async #acquireDesired(
    key: HostKey,
    generation: number,
    supplied:
      | {
          readonly id: HostId;
          readonly checkout: string | null;
          readonly revision: CheckoutRevision | undefined;
        }
      | undefined,
  ): Promise<void> {
    const id = supplied?.id ?? hostFromKey(key);
    const revision = supplied === undefined ? this.#checkoutRevisions.get(key) : supplied.revision;
    const checkout = supplied === undefined ? await this.checkout(id) : supplied.checkout;
    if (!this.#current(key, generation) || this.#checkoutRevisions.get(key) !== revision) return;
    if (checkout === null) {
      this.#watchIntents.delete(key);
      const watched = this.#watched.get(key);
      if (watched !== undefined) {
        this.#watched.delete(key);
        this.#retireWatched(key, watched);
      }
      return;
    }
    const watched = this.#watched.get(key);
    const pending = this.#watchIntents.get(key);
    // A matching replacement is already converging this watcher. Reconciliation must not wait on
    // its retirement barrier: desired-set updates remain independent of per-host replacement I/O.
    if (pending?.checkout === checkout) return;
    if (pending === undefined && watched?.checkout === checkout) return;
    this.#replaceWatched(id, checkout, this.#claimWatch(key, checkout));
  }

  /** Repair one desired host without coupling its caller to unrelated connection I/O. */
  async #repairDesired(key: HostKey): Promise<void> {
    const generation = this.#generation;
    if (!this.#current(key, generation)) return;
    try {
      await this.#acquireDesired(key, generation, undefined);
    } catch (error) {
      this.#logConnectionFailure(key, error);
    }
  }

  #logConnectionFailure(key: HostKey, error: unknown): void {
    try {
      this.#deps.log(
        `could not connect ${hostLabel(hostFromKey(key))}: ${
          error instanceof Error ? error.message : String(error)
        }`,
      );
    } catch {
      // A diagnostic must not replace the operation failure that triggered reconciliation.
    }
  }

  #current(key: HostKey, generation: number): boolean {
    return generation === this.#generation && this.#desired().has(key);
  }

  #commitCheckout(key: HostKey, checkout: string): WatchIntent {
    const revision = {checkout};
    this.#checkoutRevisions.set(key, revision);
    return revision;
  }

  async #refresh(id: HostId, revision: WatchIntent): Promise<void> {
    const key = hostKey(id);
    this.#watchIntents.set(key, revision);
    this.#replaceWatched(id, revision.checkout, revision);
    // An older desired-set pass may be waiting on this host before reaching later hosts. Start a
    // current pass without invalidating the older one; per-host intents coalesce their overlap.
    await this.#reconcile(this.#generation);
  }

  #claimWatch(key: HostKey, checkout: string): WatchIntent {
    const intent = {checkout};
    this.#watchIntents.set(key, intent);
    return intent;
  }

  /** Watch `id` from `checkout` instead of whatever it was watched from, if `intent` still holds. */
  #replaceWatched(id: HostId, checkout: string, intent: WatchIntent): void {
    const key = hostKey(id);
    try {
      if (this.#watchIntents.get(key) !== intent || !this.#desired().has(key)) return;
      const previous = this.#watched.get(key);
      previous?.stop();
      this.#watched.delete(key);
      this.#install(key, checkout);
    } finally {
      if (this.#watchIntents.get(key) === intent) this.#watchIntents.delete(key);
    }
  }

  #runReads(): RegistryRunReads {
    const reads = this.#deps.runReads;
    if (reads === undefined) throw new Error('run reads registry is missing');
    return reads;
  }

  #catalog(): RegistryRunCatalog {
    const catalog = this.#deps.catalog;
    if (catalog === undefined) throw new Error('host catalog registry is missing');
    return catalog;
  }

  /** The setup port. A missing one is a wiring error, not a reason to use the helper. */
  #setup(): RegistrySetup {
    const setup = this.#deps.setup;
    if (setup === undefined) {
      throw new Error('the registry setup is missing');
    }
    return setup;
  }

  async #inspectForCheckout(
    id: HostId,
    checkout: string,
    sequence: number,
    operation: CheckoutChange,
  ): Promise<string> {
    if (sequence <= 0) throw new Error('stale selection');
    const inspected = await this.#setup().inspectCandidate(
      id,
      checkout,
      BigInt(sequence),
      operation.signal,
    );
    operation.assertCurrent();
    if (inspected.state === 'not_found') throw new Error('the checkout was not found');
    if (inspected.state === 'too_old') throw new Error('the checkout is too old');
    if (!inspected.compatible) throw new Error('the checkout is incompatible');
    return inspected.root;
  }
}

/** One inspector's hold on a host's logs channel: its connection lease, then its logs lease. */
class InspectorLogs {
  readonly host: HostKey;
  readonly #held: Promise<{readonly connection: ConnectionLease; readonly logs: LogsLease}>;
  /** The view's reads in flight, by the token it named each with, for its `withdraw`. */
  readonly #reads = new Map<string, AbortController>();

  constructor(host: HostKey, connection: Promise<ConnectionLease>) {
    this.host = host;
    this.#held = connection.then(lease => ({connection: lease, logs: lease.connection.openLogs()}));
  }

  async lease(): Promise<LogsLease> {
    return (await this.#held).logs;
  }

  /**
   * Run the view's read named `token` under a signal `withdraw` aborts. A withdrawn read answers a
   * `transport` failure: the view stopped waiting for it, so this only settles the invoke.
   */
  async read(
    token: string,
    read: (signal: AbortSignal) => Promise<LogReadOutcome>,
  ): Promise<LogReadOutcome> {
    if (this.#reads.has(token)) {
      return {
        ok: false,
        failure: {code: 'transport', message: `read ${token} is already in flight`},
      };
    }
    const controller = new AbortController();
    this.#reads.set(token, controller);
    try {
      return await read(controller.signal);
    } catch (error) {
      if (!controller.signal.aborted) throw error;
      return {ok: false, failure: {code: 'transport', message: 'the view withdrew the read'}};
    } finally {
      if (this.#reads.get(token) === controller) this.#reads.delete(token);
    }
  }

  /** Abort the view's read in flight named `token`, if there is one. */
  withdraw(token: string): void {
    this.#reads.get(token)?.abort(new Error('the view withdrew the read'));
  }

  /**
   * Abort the reads still in flight, then release the logs lease, then the connection; a hold that
   * never connected has nothing to free.
   */
  async release(): Promise<void> {
    for (const read of this.#reads.values()) read.abort(new Error('the logs were released'));
    this.#reads.clear();
    let held: {readonly connection: ConnectionLease; readonly logs: LogsLease};
    try {
      held = await this.#held;
    } catch {
      return;
    }
    try {
      await held.logs[Symbol.asyncDispose]();
    } finally {
      await held.connection[Symbol.asyncDispose]();
    }
  }
}

interface WatchedHost {
  /** The configured checkout this watch represents, not the helper's normalized physical root. */
  readonly checkout: string;
  /** Ask the host's checkout again whether it can serve this app (the user's retry). */
  readonly recheck: () => void;
  /** Ask the host again from the start of what stopped it (the user's retry). */
  readonly retry: () => void;
  readonly stop: () => void;
}

/** One per-host acquisition's permission to replace the current watcher. Identity is the fence. */
interface WatchIntent {
  readonly checkout: string;
}

/** One checkout mutation after its settings commit; object identity fences older acquisitions. */
type CheckoutRevision = WatchIntent | {readonly removed: true};

/** One checkout validation and the permission it must still own at the settings commit boundary. */
class CheckoutChange {
  readonly host: HostKey;
  readonly settled: Promise<void>;
  #resolveSettled: () => void = () => undefined;
  #state: 'current' | 'cancelled' | 'committed' = 'current';

  readonly #abort = new AbortController();

  constructor(host: HostKey) {
    this.host = host;
    this.settled = new Promise(resolve => {
      this.#resolveSettled = resolve;
    });
  }

  readonly isCurrent = (): boolean => this.#state === 'current';

  get signal(): AbortSignal {
    return this.#abort.signal;
  }

  cancel(): boolean {
    if (this.#state !== 'current') return false;
    this.#state = 'cancelled';
    this.#abort.abort();
    return true;
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
}

function checkoutChangeKey(change: CheckoutChangeId): string {
  return `${change.owner}\0${change.sequence}`;
}

/** A helper's refusal in plain words: what failed, and the helper's own reason. */
function plain(error: unknown, what: string): Error {
  if (error instanceof HelperRequestError) return new Error(`${what}: ${error.message}`);
  return error instanceof Error ? error : new Error(String(error));
}
