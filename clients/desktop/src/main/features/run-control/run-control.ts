/**
 * The run the window is attached to, and what the user does to runs: attach, start, resume, stop,
 * and retry. Every action is a typed request on the host's connection (`run.start`, `run.stop`,
 * `instances.list`); the run view's connections are run channels the same connection opens.
 *
 * The run's identity comes from its record (`run_id`), when the app starts or attaches to it; it is
 * never inferred later. Whether the run ended comes from the host's pushed registry, a run channel
 * that ended with `run_gone`, or a check that found the run gone. The page keeps its own resume
 * logic but no retry timer: a dial waits for the host connection to be ready, and the page is woken
 * when the connection recovers.
 */
import type {
  DetachedLaunchFailure,
  InstanceList,
  LiveInstanceRecord,
} from '@vibesys/backend-client/host-helper';
import type {ConnectionStatus} from '../../../common/connection-status.js';
import {type HostId, hostFromKey, hostKey, hostLabel} from '../../../common/hosts.js';
import type {ResumeRunId} from '../../../common/resume-run.js';
import type {StopView} from '../../../common/stop-run.js';
import type {ChromeState, StartOutcome} from '../../../common/welcome-protocol.js';
import {connectionStatus, liveRecord} from '../../entities/listing.js';
import type {HostConnection, Phase} from '../../platform/connection/index.js';
import type {RecentRun} from '../../platform/settings/index.js';
import {type RelayPort, relay} from './relay.js';
import {StopController} from './stop-controller.js';

/** The page that shows an attached run. */
interface RunPage {
  /** Tell the page its host is reachable again, so its session redials now. */
  wake(): void;
  close(): void;
}

export interface RunControlDeps {
  /** The one connection to `id`. */
  connection(id: HostId): Promise<HostConnection>;
  /** Open a new run page for a run on `hostLabel`, replacing the previous one. */
  openPage(hostLabel: string): RunPage;
  /** Ask the user; resolves true on "yes". */
  confirm(text: string): Promise<boolean>;
  /** Record a run the user opened (the settings store is the one writer). */
  remember(run: RecentRun): Promise<void>;
  /** The window's state changed: send it again. */
  changed(): void;
  readonly log: (message: string) => void;
  readonly now: () => number;
}

interface Binding {
  readonly id: HostId;
  readonly instanceId: string;
  readonly runId: string;
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
  readonly release: () => void;
}

/** What a run start asked the host for. */
type Resume = {readonly run_id: ResumeRunId} | null;

export class RunControl {
  readonly #deps: RunControlDeps;
  readonly #stops: StopController;
  /** Starts the welcome view asked for, by token; true once cancelled. */
  readonly #starts = new Map<number, {cancelled: boolean}>();
  #run: Binding | null = null;

  constructor(deps: RunControlDeps) {
    this.#deps = deps;
    this.#stops = new StopController({
      stop: async (host, instanceId, force) => {
        const connection = await deps.connection(hostFromKey(host));
        return (await connection.request({type: 'run.stop', instance_id: instanceId, force}))
          .result;
      },
      confirm: text => deps.confirm(text),
      changed: () => deps.changed(),
    });
  }

  /** The attached run, for the title strip. */
  attached(): ChromeState['attached'] {
    const run = this.#run;
    if (run === null) return null;
    return {
      host: hostKey(run.id),
      hostLabel: hostLabel(run.id),
      instanceId: run.instanceId,
      runId: run.runId,
      project: run.project,
      connection: run.status,
    };
  }

  stops(): Record<string, StopView> {
    return this.#stops.views();
  }

  /** Attach to registry run `instanceId` on `id`. */
  async attach(id: HostId, instanceId: string, task: string | null = null): Promise<void> {
    const connection = await this.#deps.connection(id);
    const {listing} = await connection.request({type: 'instances.list'});
    const record = liveRecord(listing, instanceId);
    if (record === null) throw new Error(`no live run ${instanceId} on ${hostLabel(id)}`);
    await this.#bind(id, connection, record, task);
  }

  /**
   * Start a run of `task` in `project` on `id` and attach to it. When the start was cancelled by
   * the time its record appears, the run is stopped instead.
   */
  async start(
    id: HostId,
    project: string,
    task: string | null,
    args: readonly string[],
    token: number | null = null,
  ): Promise<StartOutcome> {
    if (token !== null && this.#starts.has(token))
      throw new Error('that start was already asked for');
    const cancel = {cancelled: false};
    if (token !== null) this.#starts.set(token, cancel);
    try {
      return await this.#launch(id, project, task, args, null, cancel);
    } finally {
      if (token !== null) this.#starts.delete(token);
    }
  }

  /** Cancel the start named `token`; a start that already ended has nothing to cancel. */
  cancelStart(token: number): void {
    const cancel = this.#starts.get(token);
    if (cancel !== undefined) cancel.cancelled = true;
  }

  /** Resume run `run` of `project` on `id` and attach. */
  async resume(id: HostId, project: string, run: ResumeRunId): Promise<void> {
    await this.#launch(id, project, null, [], {run_id: run}, {cancelled: false});
  }

  /** Stop `instanceId` on `id` after asking. */
  async stop(id: HostId, instanceId: string, force: boolean): Promise<void> {
    const run = this.#run;
    const attached = run !== null && run.instanceId === instanceId ? run : null;
    const label = attached?.task ?? attached?.runId ?? instanceId;
    await this.#stops.request(
      {host: hostKey(id), hostLabel: hostLabel(id), instanceId, label},
      force,
    );
  }

  /** The user asked to retry the attached run's connection (or to sign in again). */
  retry(): void {
    this.#run?.connection.dispatch({type: 'user-retry'});
  }

  /** Relay one connection the run page opened to the attached run, over a run channel. */
  connect(port: RelayPort): void {
    const run = this.#run;
    if (run === null) {
      port.close();
      return;
    }
    void relay(port, async () => {
      try {
        return (await run.connection.openRun(run.instanceId)).stream;
      } catch (error) {
        if (error instanceof Error && 'reason' in error && error.reason === 'run-gone') {
          this.#ended(run);
        }
        throw error;
      }
    });
  }

  /** End the attached run's page; the run itself keeps going on its host. */
  detach(): void {
    const run = this.#run;
    if (run === null) return;
    this.#run = null;
    run.release();
    run.page.close();
  }

  async #launch(
    id: HostId,
    project: string,
    task: string | null,
    args: readonly string[],
    resume: Resume,
    cancel: {readonly cancelled: boolean},
  ): Promise<StartOutcome> {
    const connection = await this.#deps.connection(id);
    const {launch} = await connection.request({
      type: 'run.start',
      project,
      task,
      args: [...args],
      resume,
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
    if (cancel.cancelled) {
      // Stop only the run this start launched, never one that was already live.
      if (!alreadyLive) await this.#stopCancelled(id, record.id);
      return 'cancelled';
    }
    this.#deps.log(
      alreadyLive
        ? `the run is already live as ${record.id}; attaching to it`
        : `started run ${record.run_id} as ${record.id}`,
    );
    await this.#bind(id, connection, record, task);
    return 'started';
  }

  /** Stop the run a cancelled start launched: the user's cancel is the confirmation. */
  async #stopCancelled(id: HostId, instanceId: string): Promise<void> {
    this.#deps.log(`the start was cancelled; stopping run ${instanceId}`);
    await this.#stops.stopConfirmed({
      host: hostKey(id),
      hostLabel: hostLabel(id),
      instanceId,
      label: instanceId,
    });
  }

  async #bind(
    id: HostId,
    connection: HostConnection,
    record: LiveInstanceRecord,
    task: string | null,
  ): Promise<void> {
    await this.#remember({
      host: hostKey(id),
      project: record.project_root,
      task,
      runId: record.run_id,
      attachedAt: this.#deps.now(),
    });
    this.detach();
    const page = this.#deps.openPage(hostLabel(id));
    const releases: (() => void)[] = [];
    const binding: Binding = {
      id,
      instanceId: record.id,
      runId: record.run_id,
      project: record.project_root,
      task,
      connection,
      page,
      status: connectionStatus(connection.phase),
      seenLive: false,
      release: () => {
        for (const release of releases.splice(0)) release();
      },
    };
    this.#run = binding;
    // Subscribed once the binding exists: a listing the connection already holds arrives at once.
    releases.push(
      // The run's page dials through this connection: it stays open while the run is attached.
      connection.hold(),
      connection.onPhase(phase => this.#phase(binding, phase)),
      connection.watchInstances(listing => this.#listing(binding, listing)),
      connection.onRunUnreachable(instanceId => {
        if (instanceId === binding.instanceId) this.#ended(binding);
      }),
    );
    this.#deps.changed();
  }

  async #remember(run: RecentRun): Promise<void> {
    try {
      await this.#deps.remember(run);
    } catch (error) {
      this.#deps.log(`could not remember the run: ${(error as Error).message}`);
    }
  }

  #phase(binding: Binding, phase: Phase): void {
    if (this.#run !== binding || binding.status.kind === 'run-ended') return;
    const recovered = phase.kind === 'ready' && binding.status.kind !== 'connected';
    binding.status = connectionStatus(phase);
    // The connection recovered: the page's own session redials now.
    if (recovered) binding.page.wake();
    this.#deps.changed();
  }

  #listing(binding: Binding, listing: InstanceList): void {
    const host = hostKey(binding.id);
    this.#stops.observeLive(host, new Set(listing.instances.map(record => record.id)));
    if (this.#run !== binding) return;
    const live = liveRecord(listing, binding.instanceId) !== null;
    const ended = (listing.ended ?? []).some(record => record.id === binding.instanceId);
    if (live) binding.seenLive = true;
    else if (ended || binding.seenLive) this.#ended(binding);
  }

  #ended(binding: Binding): void {
    if (this.#run !== binding || binding.status.kind === 'run-ended') return;
    binding.status = {kind: 'run-ended'};
    this.#stops.runEnded(hostKey(binding.id), binding.instanceId);
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
