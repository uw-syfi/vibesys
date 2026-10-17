/**
 * The runs the windows are attached to, and what the user does to runs: attach, start, resume,
 * stop, and retry. Every action is a typed request on the host's connection (`run.start`,
 * `run.stop`, `instances.list`); each run page's connections are run channels the same connection
 * opens.
 *
 * A run's identity is its host and the `run_id` from its record, when the app starts or attaches to
 * it; it is never inferred later. Each attached run keeps its own binding under that composite
 * identity: attaching, starting, or resuming a run never disturbs a binding under a different key,
 * so one window's run cannot be silently detached by another window's navigation. Whether a run
 * ended comes from the host's pushed registry, a run channel that ended with `run_gone`, or a check
 * that found the run gone. The page keeps its own resume logic but no retry timer: while its page
 * still owns it, a dial waits for the host connection to be ready, and the page is woken when the
 * connection recovers.
 *
 * Navigation toward a target (a host and instance id) is tracked separately from bindings, by the
 * same key: a newer attach, start, or resume toward the *same* target supersedes an older one
 * in flight toward it (mirroring the single-window product's "latest click wins" expectation for
 * one target), but navigation toward a *different* target never touches it.
 */

import {
  type ConnectionLease,
  connectionStatus,
  type HostConnection,
  type LogsLease,
  type Phase,
} from '@vibesys/connection';
import type {
  DetachedLaunchFailure,
  InstanceList,
  LiveInstanceRecord,
} from '@vibesys/contracts/host-helper';
import {
  type ConnectionStatus,
  type HostId,
  hostFromKey,
  hostKey,
  hostLabel,
  instanceObservation,
} from '@vibesys/hosts';
import type {
  ChromeState,
  HarnessChoice,
  RunLogsReply,
  RunLogsRequest,
  StartOutcome,
} from '@vibesys/ipc';
import {type ResumeRunId, type RunRef, runRefKey, type StopView} from '@vibesys/runs';
import type {StoredRecentRun} from '@vibesys/settings';
import {type RelayPort, relay} from './relay.js';
import {StopController} from './stop-controller.js';

/** The page that shows one attached run, in its own window. */
interface RunPage {
  /** Tell the page its host is reachable again, so its session redials now. */
  wake(): void;
  close(): void;
}

export interface RunControlDeps {
  /** Lease the one connection to `id`; the caller releases or transfers every lease. */
  connection(id: HostId): Promise<ConnectionLease>;
  /** Open (or focus an already-open) window for `run`, titled from `title`. */
  openPage(run: RunRef, title: {readonly hostLabel: string; readonly task: string | null}): RunPage;
  /** Ask the user; resolves true on "yes". */
  confirm(text: string): Promise<boolean>;
  /** Record a run only while `current` says this selection still owns navigation. */
  remember(run: StoredRecentRun, current: () => boolean): Promise<void>;
  /** The window's state changed: send it again. */
  changed(): void;
  readonly log: (message: string) => void;
  readonly now: () => number;
}

interface Binding {
  readonly id: HostId;
  readonly instanceId: string;
  readonly run: RunRef;
  readonly project: string;
  readonly task: string | null;
  readonly connection: HostConnection;
  readonly page: RunPage;
  status: ConnectionStatus;
  /**
   * True once a listing showed the run's server live. A listing read before a start published its
   * record says nothing about the run, so only a server seen live, then gone, has ended.
   */
  seenLive: boolean;
  /**
   * The binding's lease on its host's logs channel, taken by the page's first logs request and
   * released with the binding.
   */
  logs: LogsLease | null;
  /** The page's log reads in flight, by the token it named each with, for its `withdraw`. */
  readonly reads: Map<string, AbortController>;
  /** Retire this binding's connection lease once and report when cleanup settles. */
  readonly release: () => PromiseLike<void>;
}

/** Release a binding's connection lease and, when its page asked for logs, its logs lease. */
function releaseLeases(lease: ConnectionLease, binding: Binding | null): PromiseLike<void> {
  const logs = binding?.logs ?? null;
  if (binding !== null) {
    binding.logs = null;
    for (const read of binding.reads.values()) read.abort(new Error('the run page was released'));
    binding.reads.clear();
  }
  const released = lease[Symbol.asyncDispose]();
  if (logs === null) return released;
  return Promise.all([logs[Symbol.asyncDispose](), released]).then(() => undefined);
}

/** What a page's log read answers. */
type LogReadReply = RunLogsReply<Extract<RunLogsRequest, {readonly kind: 'read'}>>;

/**
 * Run the page's read named `token` under a signal its `withdraw` aborts. A withdrawn read answers a
 * `transport` failure: the page stopped waiting for it, so this only settles the invoke.
 */
async function withdrawableRead(
  reads: Map<string, AbortController>,
  token: string,
  read: (signal: AbortSignal) => Promise<LogReadReply>,
): Promise<LogReadReply> {
  if (reads.has(token)) {
    return {ok: false, failure: {code: 'transport', message: `read ${token} is already in flight`}};
  }
  const controller = new AbortController();
  reads.set(token, controller);
  try {
    return await read(controller.signal);
  } catch (error) {
    if (!controller.signal.aborted) throw error;
    return {ok: false, failure: {code: 'transport', message: 'the page withdrew the read'}};
  } finally {
    if (reads.get(token) === controller) reads.delete(token);
  }
}

/** One target (host, instance) a call is navigating toward, from claim to bind or abandonment. */
interface Target {
  readonly key: string;
  readonly id: HostId;
  readonly instanceId: string;
}

/**
 * One call's claim on a target. `cancelled` is set true the moment a newer call claims the same
 * target (superseding this one), or this call is explicitly withdrawn (`cancelStart`, a failure).
 * `target` is null until the call's eventual (host, instance) is known: immediate for `attach`,
 * learned only once `run.start`/`resume` answers for `start`/`resume`.
 *
 * `sequence` orders claims by when the user asked (`attach`/`start`/`resume` was called), not by
 * when each learns its target: a `start` only learns its target once `run.start` answers, which may
 * land after a later `attach` already claimed the same target. Comparing `sequence` at the moment a
 * target is claimed, rather than trusting whichever call reaches `#claimTarget` first, keeps
 * "the newer request wins" true regardless of that race.
 */
interface Claim {
  cancelled: boolean;
  target: Target | null;
  readonly sequence: number;
}

/** One welcome document's start request, unique across renderer-document lifetimes. */
export interface StartIdentity {
  readonly owner: string;
  readonly sequence: number;
}

/** One stale launch waiting to learn whether the newer claim successfully adopts its server. */
interface PendingAdoption {
  readonly result: Promise<boolean>;
  settle(adopted: boolean): void;
}

/** What a run start asked the host for. */
type Resume = {readonly run_id: ResumeRunId} | null;

/** What one `run.start` asks for besides its project. */
interface Launch {
  readonly task: string | null;
  readonly args: readonly string[];
  readonly resume: Resume;
  /** A pair the host's `harnesses.list` offered; null keeps the project's configuration. */
  readonly harness: HarnessChoice | null;
}

export class RunControl {
  readonly #deps: RunControlDeps;
  readonly #stops: StopController;
  /** Every run bound to a window, by `(host, run_id)`. */
  readonly #bindings = new Map<string, Binding>();
  /** The claim currently navigating toward (or already bound to) each target, by `targetKey`. */
  readonly #claims = new Map<string, Claim>();
  /** Starts the welcome view asked for, by document owner and renderer-local sequence. */
  readonly #starts = new Map<string, Map<number, Claim>>();
  /** Stale launches held until the matching claim binds or is abandoned. */
  readonly #pendingAdoptions = new Map<string, PendingAdoption>();
  /**
   * The most recently bound run, for the single legacy "attached run" the welcome window's title
   * strip still shows (`ChromeState.attached`). A real multi-run summary is the unified run-registry
   * dashboard's job (#814/#816), out of scope here; until then this is a deliberate, documented
   * simplification, not a correctness property of the registry itself.
   */
  #lastBoundRun: RunRef | null = null;
  /** Orders claims by request time; see `Claim.sequence`. */
  #nextSequence = 0;

  constructor(deps: RunControlDeps) {
    this.#deps = deps;
    this.#stops = new StopController({
      stop: async (host, instanceId, force, current) => {
        await using lease = await deps.connection(hostFromKey(host));
        const attempt = await lease.connection.requestIfCurrent(
          {type: 'run.stop', instance_id: instanceId, force},
          current,
        );
        switch (attempt.kind) {
          case 'not-written':
            return attempt;
          case 'answered':
            return {kind: 'answered', result: attempt.reply.result};
          case 'failed':
            return {
              kind: 'failed',
              written: attempt.written,
              message:
                attempt.error instanceof Error ? attempt.error.message : String(attempt.error),
            };
        }
      },
      confirm: text => deps.confirm(text),
      changed: () => deps.changed(),
    });
  }

  /** The most recently bound run, for the title strip. */
  attached(): ChromeState['attached'] {
    const ref = this.#lastBoundRun;
    const run = ref === null ? undefined : this.#bindings.get(runRefKey(ref));
    if (run === undefined) return null;
    return {
      host: hostKey(run.id),
      hostLabel: hostLabel(run.id),
      instanceId: run.instanceId,
      runId: run.run.runId,
      project: run.project,
      connection: run.status,
    };
  }

  stops(): Record<string, StopView> {
    return this.#stops.views();
  }

  /** Attach to registry run `instanceId` on `id`. */
  async attach(id: HostId, instanceId: string, task: string | null = null): Promise<void> {
    const claim: Claim = this.#newClaim();
    this.#claimTarget(id, instanceId, claim);
    let lease: ConnectionLease | null = null;
    try {
      lease = await this.#deps.connection(id);
      const connection = lease.connection;
      if (claim.cancelled) return;
      const {listing} = await connection.request({type: 'instances.list'});
      if (claim.cancelled) return;
      const observation = instanceObservation(listing, instanceId);
      switch (observation.kind) {
        case 'live': {
          const bindingLease = lease;
          lease = null;
          await this.#bind(claim, id, bindingLease, observation.record, task);
          break;
        }
        case 'unverified':
          throw new Error(
            `${hostLabel(id)} could not confirm whether run ${instanceId} is still live`,
          );
        case 'unreadable':
          throw new Error(
            `run ${instanceId} on ${hostLabel(id)} cannot be read by this VibeSys version`,
          );
        case 'ended':
        case 'absent':
          throw new Error(`no live run ${instanceId} on ${hostLabel(id)}`);
      }
    } catch (error) {
      this.#forgetClaim(claim);
      throw error;
    } finally {
      await lease?.[Symbol.asyncDispose]();
    }
  }

  /**
   * Start a run of `task` in `project` on `id` and attach to it, with `harness` (one the host's
   * `harnesses.list` offered; null keeps the project's configuration). When the start was cancelled
   * by the time its record appears, the run is stopped instead.
   */
  async start(
    id: HostId,
    project: string,
    task: string | null,
    args: readonly string[],
    identity: StartIdentity | null = null,
    harness: HarnessChoice | null = null,
  ): Promise<StartOutcome> {
    if (identity !== null && this.#startClaim(identity) !== undefined)
      throw new Error('that start was already asked for');
    const claim: Claim = this.#newClaim();
    if (identity !== null) this.#setStartClaim(identity, claim);
    try {
      return await this.#launch(id, project, {task, args, resume: null, harness}, claim);
    } finally {
      if (identity !== null) this.#deleteStartClaim(identity, claim);
    }
  }

  /** Cancel the start named by `identity`; a start that already ended has nothing to cancel. */
  cancelStart(identity: StartIdentity): void {
    const claim = this.#startClaim(identity);
    if (claim === undefined) return;
    claim.cancelled = true;
    this.#forgetClaim(claim);
  }

  /** Revoke every pending start owned by a renderer document that went away. */
  retireStartOwner(owner: string): void {
    const starts = this.#starts.get(owner);
    if (starts === undefined) return;
    for (const claim of starts.values()) {
      claim.cancelled = true;
      this.#forgetClaim(claim);
    }
  }

  /** Resume run `run` of `project` on `id` and attach. */
  async resume(id: HostId, project: string, run: ResumeRunId): Promise<void> {
    const launch: Launch = {task: null, args: [], resume: {run_id: run}, harness: null};
    await this.#launch(id, project, launch, this.#newClaim());
  }

  /** Stop `instanceId` on `id` after asking, unless the view that asked was already `confirmed`. */
  async stop(id: HostId, instanceId: string, force: boolean, confirmed: boolean): Promise<void> {
    const run = this.#bindingFor(id, instanceId);
    const label = run?.task ?? run?.run.runId ?? instanceId;
    await this.#stops.request(
      {host: hostKey(id), hostLabel: hostLabel(id), instanceId, label},
      force,
      confirmed,
    );
  }

  /** The user asked to retry the most recently bound run's connection (or to sign in again). */
  retry(): void {
    const run = this.#lastBoundRun;
    if (run === null) return;
    this.#bindings.get(runRefKey(run))?.connection.dispatch({type: 'user-retry'});
  }

  /**
   * Relay one connection `run`'s run page opened, over a run channel. Message and close listener
   * ownership is installed synchronously before this returns.
   */
  connect(port: RelayPort, ref: RunRef): void {
    const run = this.#bindings.get(runRefKey(ref));
    if (run === undefined) {
      port.close();
      return;
    }
    void relay(port, async signal => {
      try {
        return (await run.connection.openRun(run.instanceId, signal)).stream;
      } catch (error) {
        if (error instanceof Error && 'reason' in error) {
          if (error.reason === 'run-gone') this.#ended(run);
          else if (error.reason === 'capacity') this.#capacityRefused(run, error.message);
        }
        throw error;
      }
    });
  }

  /**
   * Answer one logs request from `run`'s run page, over its host's logs channel. The run's
   * instance id comes from the binding, which took it from the record the app attached to: a
   * listing names it, and a read that names another instance is refused. A `withdraw` aborts the
   * page's read in flight with that token, which withdraws the helper's long poll; a token naming
   * no read in flight (it already answered) is a no-op.
   */
  logs<R extends RunLogsRequest>(run: RunRef, request: R): Promise<RunLogsReply<R>>;
  async logs(ref: RunRef, request: RunLogsRequest): Promise<RunLogsReply<RunLogsRequest>> {
    const run = this.#bindings.get(runRefKey(ref));
    if (request.kind === 'withdraw') {
      run?.reads.get(request.token)?.abort(new Error('the page withdrew the read'));
      return null;
    }
    if (run === undefined) {
      return {
        ok: false,
        failure: {code: 'unknown_instance', message: `run ${ref.runId} is not attached`},
      };
    }
    run.logs ??= run.connection.openLogs();
    if (request.kind === 'list') return run.logs.list(run.instanceId);
    if (request.read.instance_id !== run.instanceId) {
      return {
        ok: false,
        failure: {
          code: 'unknown_instance',
          message: `this page is bound to instance ${run.instanceId}, not ${request.read.instance_id}`,
        },
      };
    }
    const logs = run.logs;
    return withdrawableRead(run.reads, request.token, signal => logs.read(request.read, signal));
  }

  /** End `run`'s page; the run itself keeps going on its host. Safe when it is not bound. */
  detach(run: RunRef): void {
    this.#releaseBinding(run);
  }

  /** Drop `ref`'s binding, without disturbing any other binding or in-flight navigation. */
  #releaseBinding(ref: RunRef): void {
    const key = runRefKey(ref);
    const run = this.#bindings.get(key);
    if (run === undefined) return;
    this.#bindings.delete(key);
    if (this.#lastBoundRun !== null && runRefKey(this.#lastBoundRun) === key) {
      this.#lastBoundRun = null;
    }
    const target = targetKey(run.id, run.instanceId);
    if (this.#claims.get(target) !== undefined) this.#claims.delete(target);
    this.#settleAdoption(run.id, run.instanceId, false);
    let retirement: PromiseLike<void>;
    try {
      retirement = run.release();
    } catch (error) {
      this.#logRetirementFailure(run, error);
      run.page.close();
      return;
    }
    void Promise.resolve(retirement).catch(error => this.#logRetirementFailure(run, error));
    run.page.close();
  }

  /** Observe cleanup without extending a synchronous navigation transition into resource I/O. */
  #logRetirementFailure(binding: Binding, error: unknown): void {
    try {
      this.#deps.log(
        `could not retire run ${binding.instanceId} on ${hostLabel(binding.id)}: ${
          error instanceof Error ? error.message : String(error)
        }`,
      );
    } catch {
      // Diagnostics cannot become a second unhandled failure after the manager retained its barrier.
    }
  }

  async #launch(id: HostId, project: string, run: Launch, claim: Claim): Promise<StartOutcome> {
    const {task, args, resume, harness} = run;
    let lease: ConnectionLease | null = await this.#deps.connection(id);
    try {
      const connection = lease.connection;
      if (claim.cancelled) return 'cancelled';
      const {launch} = await connection.request({
        type: 'run.start',
        project,
        task,
        args: [...args],
        resume,
        // Sent only when chosen: a helper without the `harnesses` feature never sees the field.
        ...(harness === null ? {} : {harness: {provider: harness.provider, model: harness.model}}),
      });
      let record: LiveInstanceRecord;
      let alreadyLive = false;
      if (isFailure(launch)) {
        if (launch.live_instance == null) throw new Error(failureMessage(launch));
        // `--resume` of a run another server drives: attach to that one instead.
        record = launch.live_instance;
        alreadyLive = true;
      } else {
        record = launch;
      }
      if (claim.cancelled) {
        // Stop only the run this launch created, never one that was already live.
        if (!alreadyLive) await this.#stopSuperseded(id, record, true);
        return 'cancelled';
      }
      this.#claimTarget(id, record.id, claim);
      this.#deps.log(
        alreadyLive
          ? `the run is already live as ${record.id}; attaching to it`
          : `started run ${record.run_id} as ${record.id}`,
      );
      const bindingLease = lease;
      lease = null;
      const bound = await this.#bind(claim, id, bindingLease, record, task);
      if (!bound) {
        if (!alreadyLive) await this.#stopSuperseded(id, record, claim.cancelled);
        return 'cancelled';
      }
      return 'started';
    } catch (error) {
      this.#forgetClaim(claim);
      throw error;
    } finally {
      await lease?.[Symbol.asyncDispose]();
    }
  }

  /** Stop the unattached run this launch created: cancel or supersession is the confirmation. */
  async #stopSuperseded(id: HostId, record: LiveInstanceRecord, cancelled: boolean): Promise<void> {
    const reason = cancelled ? 'the start was cancelled' : 'a newer run selection won';
    for (;;) {
      if (this.#isAttached(id, record.id)) {
        this.#deps.log(`${reason}; run ${record.id} was selected by the newer intent`);
        return;
      }
      if (this.#isIntended(id, record.id)) {
        this.#deps.log(`${reason}; waiting for the newer selection of run ${record.id}`);
        if (await this.#adoption(id, record.id)) {
          this.#deps.log(`${reason}; run ${record.id} was selected by the newer intent`);
          return;
        }
        continue;
      }

      const current = (): boolean => !this.#isTargeted(id, record.id);
      if (!current()) continue;
      this.#deps.log(`${reason}; stopping run ${record.id}`);
      const result = await this.#stops.stopConfirmed(
        {
          host: hostKey(id),
          hostLabel: hostLabel(id),
          instanceId: record.id,
          label: record.run_id,
        },
        current,
      );
      if (result.kind === 'withdrawn') continue;
      if (result.kind === 'failed')
        this.#deps.log(`could not stop superseded run ${record.id}: ${result.message}`);
      return;
    }
  }

  /** True when a target is bound, or a live claim is still navigating toward it. */
  #isTargeted(id: HostId, instanceId: string): boolean {
    return this.#isAttached(id, instanceId) || this.#isIntended(id, instanceId);
  }

  #isAttached(id: HostId, instanceId: string): boolean {
    return this.#bindingFor(id, instanceId) !== undefined;
  }

  #bindingFor(id: HostId, instanceId: string): Binding | undefined {
    for (const binding of this.#bindings.values()) {
      if (binding.instanceId === instanceId && hostKey(binding.id) === hostKey(id)) return binding;
    }
    return undefined;
  }

  /** Whether `binding` is still the installed owner of its composite run identity. */
  #ownsBinding(binding: Binding): boolean {
    return this.#bindings.get(runRefKey(binding.run)) === binding;
  }

  #isIntended(id: HostId, instanceId: string): boolean {
    const claim = this.#claims.get(targetKey(id, instanceId));
    return claim !== undefined && !claim.cancelled;
  }

  /** Wait for the current matching claim to bind or abandon this target. */
  #adoption(id: HostId, instanceId: string): Promise<boolean> {
    if (this.#isAttached(id, instanceId)) return Promise.resolve(true);
    if (!this.#isIntended(id, instanceId)) return Promise.resolve(false);
    const key = targetKey(id, instanceId);
    const existing = this.#pendingAdoptions.get(key);
    if (existing !== undefined) return existing.result;
    let settle = (_adopted: boolean): void => {};
    const pending: PendingAdoption = {
      result: new Promise<boolean>(resolve => {
        settle = resolve;
      }),
      settle: adopted => settle(adopted),
    };
    this.#pendingAdoptions.set(key, pending);
    return pending.result;
  }

  #settleAdoption(id: HostId, instanceId: string, adopted: boolean): void {
    const key = targetKey(id, instanceId);
    const pending = this.#pendingAdoptions.get(key);
    if (pending === undefined) return;
    this.#pendingAdoptions.delete(key);
    pending.settle(adopted);
  }

  async #bind(
    claim: Claim,
    id: HostId,
    lease: ConnectionLease,
    record: LiveInstanceRecord,
    task: string | null,
  ): Promise<boolean> {
    const run: RunRef = {host: hostKey(id), runId: record.run_id};
    const connection = lease.connection;
    const subscriptions: (() => void)[] = [];
    const pendingObservations: (() => void)[] = [];
    let observationPhase: 'buffering' | 'live' | 'closed' = 'buffering';
    let page: RunPage | null = null;
    let pageClosed = false;
    let leaseRelease: PromiseLike<void> | null = null;
    let binding: Binding | null = null;
    let installed = false;
    const observe = (observation: () => void): void => {
      if (observationPhase === 'live') observation();
      else if (observationPhase === 'buffering') pendingObservations.push(observation);
    };
    const closePage = (): void => {
      if (pageClosed || page === null) return;
      pageClosed = true;
      page.close();
    };
    const release = (): PromiseLike<void> => {
      if (leaseRelease !== null) return leaseRelease;
      observationPhase = 'closed';
      pendingObservations.splice(0);
      for (const unsubscribe of subscriptions.splice(0).reverse()) unsubscribe();
      try {
        leaseRelease = releaseLeases(lease, binding);
      } catch (error) {
        leaseRelease = Promise.reject(error);
      }
      return leaseRelease;
    };
    try {
      if (claim.cancelled) return false;
      await this.#remember(claim, {
        host: hostKey(id),
        project: record.project_root,
        task,
        runId: record.run_id,
        instanceId: record.id,
        attachedAt: this.#deps.now(),
      });
      if (claim.cancelled) return false;
      this.#releaseBinding(run);
      page = this.#deps.openPage(run, {hostLabel: hostLabel(id), task});
      const provisional: Binding = {
        id,
        instanceId: record.id,
        run,
        project: record.project_root,
        task,
        connection,
        page: {wake: () => page?.wake(), close: closePage},
        status: connectionStatus(connection.phase),
        seenLive: false,
        logs: null,
        reads: new Map(),
        release,
      };
      binding = provisional;
      // Buffer synchronous initial observations until every subscription returned its cleanup.
      subscriptions.push(
        connection.onPhase(phase => observe(() => this.#phase(provisional, phase))),
      );
      subscriptions.push(
        connection.watchInstances(listing => observe(() => this.#listing(provisional, listing))),
      );
      subscriptions.push(
        connection.onRunUnreachable(instanceId => {
          observe(() => {
            if (instanceId === provisional.instanceId) this.#ended(provisional);
          });
        }),
      );
      this.#bindings.set(runRefKey(run), provisional);
      this.#lastBoundRun = run;
      observationPhase = 'live';
      for (const observation of pendingObservations.splice(0)) observation();
      this.#deps.changed();
      if (claim.cancelled || !this.#ownsBinding(provisional)) return false;
      installed = true;
      this.#settleAdoption(id, record.id, true);
      return true;
    } finally {
      if (!installed) {
        observationPhase = 'closed';
        if (binding !== null && this.#ownsBinding(binding)) this.#bindings.delete(runRefKey(run));
        try {
          leaseRelease = release();
        } finally {
          closePage();
          this.#forgetClaim(claim);
        }
        await leaseRelease;
      }
    }
  }

  async #remember(claim: Claim, run: StoredRecentRun): Promise<void> {
    try {
      await this.#deps.remember(run, () => !claim.cancelled);
    } catch (error) {
      this.#deps.log(`could not remember the run: ${(error as Error).message}`);
    }
  }

  /** A fresh claim, sequenced by request time so a later race can tell who asked more recently. */
  #newClaim(): Claim {
    return {cancelled: false, target: null, sequence: this.#nextSequence++};
  }

  /**
   * Register `claim` as the live navigation toward (`id`, `instanceId`), superseding whatever claim
   * held that exact target before (an older in-flight call, or an already-bound run at that same
   * target) — never a claim toward any other target.
   *
   * The claim already holding the target may have reached `#claimTarget` after `claim` did (a
   * `start`/`resume` only learns its target once `run.start` answers, which can land after a later
   * `attach` already claimed it): comparing `sequence`, not arrival order, is what keeps the claim
   * from the more recent request the winner either way.
   */
  #claimTarget(id: HostId, instanceId: string, claim: Claim): void {
    if (claim.cancelled) return;
    const key = targetKey(id, instanceId);
    const previous = this.#claims.get(key);
    if (previous !== undefined && previous !== claim && previous.sequence > claim.sequence) {
      // The claim already there asked more recently than this one: this call lost the race before
      // it ever held the target, so it is the one superseded, not the other way around.
      claim.cancelled = true;
      return;
    }
    claim.target = {key, id, instanceId};
    this.#claims.set(key, claim);
    if (previous !== undefined && previous !== claim) {
      previous.cancelled = true;
      this.#settleAdoption(id, instanceId, false);
    }
  }

  /** A claim that failed, was cancelled, or lost its race no longer protects its target. */
  #forgetClaim(claim: Claim): void {
    const target = claim.target;
    if (target === null) return;
    if (this.#claims.get(target.key) !== claim) return;
    this.#claims.delete(target.key);
    this.#settleAdoption(target.id, target.instanceId, false);
  }

  #startClaim(identity: StartIdentity): Claim | undefined {
    return this.#starts.get(identity.owner)?.get(identity.sequence);
  }

  #setStartClaim(identity: StartIdentity, claim: Claim): void {
    const starts = this.#starts.get(identity.owner) ?? new Map<number, Claim>();
    starts.set(identity.sequence, claim);
    this.#starts.set(identity.owner, starts);
  }

  #deleteStartClaim(identity: StartIdentity, claim: Claim): void {
    const starts = this.#starts.get(identity.owner);
    if (starts?.get(identity.sequence) !== claim) return;
    starts.delete(identity.sequence);
    if (starts.size === 0) this.#starts.delete(identity.owner);
  }

  #phase(binding: Binding, phase: Phase): void {
    if (!this.#ownsBinding(binding) || binding.status.kind === 'run-ended') {
      return;
    }
    const recovered = phase.kind === 'ready' && binding.status.kind !== 'connected';
    binding.status = connectionStatus(phase);
    // The connection recovered: the page's own session redials now.
    if (recovered) binding.page.wake();
    this.#deps.changed();
  }

  #listing(binding: Binding, listing: InstanceList): void {
    const host = hostKey(binding.id);
    this.#stops.observeListing(host, listing);
    if (!this.#ownsBinding(binding)) return;
    const observation = instanceObservation(listing, binding.instanceId);
    switch (observation.kind) {
      case 'live':
        binding.seenLive = true;
        break;
      case 'ended':
        this.#ended(binding);
        break;
      case 'absent':
        if (binding.seenLive) this.#ended(binding);
        break;
      case 'unverified':
      case 'unreadable':
        break;
    }
  }

  #ended(binding: Binding): void {
    if (!this.#ownsBinding(binding) || binding.status.kind === 'run-ended') {
      return;
    }
    binding.status = {kind: 'run-ended'};
    this.#stops.runEnded(hostKey(binding.id), binding.instanceId);
    this.#deps.changed();
  }

  /** This window's own dial to its run was refused: the host's channel ceiling is spent. */
  #capacityRefused(binding: Binding, detail: string): void {
    if (!this.#ownsBinding(binding) || binding.status.kind === 'run-ended') {
      return;
    }
    binding.status = {kind: 'capacity', detail};
    this.#deps.changed();
  }
}

function isFailure(
  launch: LiveInstanceRecord | DetachedLaunchFailure,
): launch is DetachedLaunchFailure {
  return 'outcome' in launch || 'exit_code' in launch;
}

/** What a user reads about a launch that started nothing. */
function failureMessage(failure: DetachedLaunchFailure): string {
  const log = failure.log_path == null ? '' : ` Its log is ${failure.log_path}.`;
  return `${failure.message} (${failure.code} at ${failure.stage}).${log}`;
}

function targetKey(id: HostId, instanceId: string): string {
  return `${hostKey(id)}\n${instanceId}`;
}
