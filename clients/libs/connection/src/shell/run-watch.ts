/** One WatchRuns stream per host.

Cancelling aborts the stream. The gateway closes its watcher when that stream
ends. A late frame after cancel is ignored.

The stream is the host's one source of its runs: `live` counts the runs the host says are alive,
and `observe` hands every observer the runs and the stream's own phase. An observed host reconnects
with a growing delay when its stream ends or goes silent for longer than the protocol's lease; an
unobserved one is retired and reopens on the next `start`, `live` or `observe`.
*/
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {type HostId, type HostKey, hostKey} from '@vibesys/hosts';
import {
  type RunHead,
  RunLiveness,
  WatchRunsRequestSchema,
  WatchRunsResponseSchema,
} from '@vibesys/proto';
import type {ScheduleTimeout} from '@vibesys/transport';

export interface RunWatchPorts {
  stream(
    host: HostId,
    bytes: Uint8Array,
    signal: AbortSignal,
    receive: (bytes: Uint8Array) => void,
  ): Promise<void>;
}

/** What the host last said of one run: its head (absent for a phase 1 run) and its liveness. */
export interface WatchedRun {
  readonly head: RunHead | undefined;
  readonly liveness: RunLiveness;
}

/** Where the host's stream stands, as its observers hear it. */
export type RunsPhase =
  /** The initial runs have not all arrived. */
  | {readonly kind: 'connecting'}
  | {readonly kind: 'live'}
  /** The stream ended or went silent; `attempt` counts the failures since it last delivered. */
  | {readonly kind: 'reconnecting'; readonly attempt: number; readonly detail: string};

export interface RunsState {
  readonly phase: RunsPhase;
  /** The runs the host has announced, by run id. Meaningful once `phase` has been `live`. */
  readonly runs: ReadonlyMap<string, WatchedRun>;
}

export interface RunWatch {
  start(host: HostId): void;
  cancel(host: HostId): void;
  close(): void;
  live(host: HostId): Promise<number>;
  /**
   * Opens the host's stream if it is not open and tells `listener` the state now and on each change
   * after it. The returned function removes the listener; `cancel` ends the stream.
   */
  observe(host: HostId, listener: (state: RunsState) => void): () => void;
}

/** The protocol's lease: a receiver expires its facts after thirty seconds without a frame. */
export const RUN_WATCH_LEASE_MS = 30_000;
/** The delay before the first reconnect; it doubles up to `RECONNECT_MAX_MS`. */
const RECONNECT_FIRST_MS = 1_000;
const RECONNECT_MAX_MS = 30_000;

interface HostRuns {
  readonly host: HostId;
  abort: AbortController;
  readonly runs: Map<string, WatchedRun>;
  ready: boolean;
  readonly waiters: ((count: number) => void)[];
  readonly listeners: Set<(state: RunsState) => void>;
  failures: number;
  detail: string;
  cancelLease: (() => void) | null;
  cancelRetry: (() => void) | null;
}

/** Fold WatchRuns for the hosts this app is showing. */
export function runWatch(ports: RunWatchPorts, schedule: ScheduleTimeout): RunWatch {
  const hosts = new Map<HostKey, HostRuns>();
  const context = {hosts, ports, schedule};
  return {
    start(host) {
      open(context, host);
    },
    cancel(host) {
      stop(context, hostKey(host));
    },
    close() {
      for (const key of [...hosts.keys()]) stop(context, key);
    },
    live(host) {
      const opened = open(context, host);
      if (opened.ready) return Promise.resolve(alive(opened));
      return new Promise(resolve => {
        opened.waiters.push(resolve);
      });
    },
    observe(host, listener) {
      const opened = open(context, host);
      opened.listeners.add(listener);
      listener(stateOf(opened));
      return () => {
        opened.listeners.delete(listener);
      };
    },
  };
}

interface Context {
  readonly hosts: Map<HostKey, HostRuns>;
  readonly ports: RunWatchPorts;
  readonly schedule: ScheduleTimeout;
}

function open(context: Context, host: HostId): HostRuns {
  const key = hostKey(host);
  const existing = context.hosts.get(key);
  if (existing !== undefined) return existing;
  const opened: HostRuns = {
    host,
    abort: new AbortController(),
    runs: new Map(),
    ready: false,
    waiters: [],
    listeners: new Set(),
    failures: 0,
    detail: '',
    cancelLease: null,
    cancelRetry: null,
  };
  context.hosts.set(key, opened);
  connect(context, key, opened);
  return opened;
}

/** Opens one stream for `opened`; its end retires or retries the host, by whether anyone observes. */
function connect(context: Context, key: HostKey, opened: HostRuns): void {
  const attempt = new AbortController();
  opened.abort = attempt;
  const bytes = toBinary(WatchRunsRequestSchema, create(WatchRunsRequestSchema, {}));
  armLease(context, key, opened, attempt);
  void context.ports
    .stream(opened.host, bytes, attempt.signal, frame =>
      accept(context, key, opened, attempt, frame),
    )
    .then(
      () => ended(context, key, opened, attempt, 'the run stream ended'),
      (error: unknown) => ended(context, key, opened, attempt, describe(error)),
    );
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** Every frame renews the lease; a silent stream is dropped and opened again. */
function armLease(
  context: Context,
  key: HostKey,
  opened: HostRuns,
  attempt: AbortController,
): void {
  opened.cancelLease?.();
  opened.cancelLease = context.schedule(
    () => ended(context, key, opened, attempt, 'the run stream went silent'),
    RUN_WATCH_LEASE_MS,
  );
}

function accept(
  context: Context,
  key: HostKey,
  opened: HostRuns,
  attempt: AbortController,
  frame: Uint8Array,
): void {
  if (attempt.signal.aborted) return;
  armLease(context, key, opened, attempt);
  const message = fromBinary(WatchRunsResponseSchema, frame);
  opened.failures = 0;
  if (message.initialComplete) {
    settle(opened);
    notify(opened);
    return;
  }
  if (message.heartbeat) return;
  if (message.removed) opened.runs.delete(message.runId);
  else
    opened.runs.set(message.runId, {
      head: message.head,
      liveness: message.liveness,
    });
  if (opened.ready) notify(opened);
}

function settle(opened: HostRuns): void {
  if (opened.ready) return;
  opened.ready = true;
  const count = alive(opened);
  for (const waiter of opened.waiters) waiter(count);
  opened.waiters.length = 0;
}

/** The stream of `attempt` is over: an observed host opens another later, any other is retired. */
function ended(
  context: Context,
  key: HostKey,
  opened: HostRuns,
  attempt: AbortController,
  detail: string,
): void {
  // A stream that was already replaced or cancelled settles late and changes nothing.
  if (opened.abort !== attempt || context.hosts.get(key) !== opened) return;
  attempt.abort();
  opened.cancelLease?.();
  opened.cancelLease = null;
  if (opened.listeners.size === 0) {
    retire(context, key, opened);
    return;
  }
  opened.failures += 1;
  opened.detail = detail;
  opened.ready = false;
  opened.runs.clear();
  for (const waiter of opened.waiters) waiter(0);
  opened.waiters.length = 0;
  notify(opened);
  const delay = Math.min(RECONNECT_FIRST_MS * 2 ** (opened.failures - 1), RECONNECT_MAX_MS);
  opened.cancelRetry = context.schedule(() => {
    opened.cancelRetry = null;
    if (context.hosts.get(key) === opened) connect(context, key, opened);
  }, delay);
}

function retire(context: Context, key: HostKey, opened: HostRuns): void {
  if (context.hosts.get(key) === opened) context.hosts.delete(key);
  opened.abort.abort();
  opened.cancelLease?.();
  opened.cancelRetry?.();
  opened.cancelLease = null;
  opened.cancelRetry = null;
  settle(opened);
}

function stop(context: Context, key: HostKey): void {
  const opened = context.hosts.get(key);
  if (opened === undefined) return;
  opened.listeners.clear();
  retire(context, key, opened);
}

function stateOf(opened: HostRuns): RunsState {
  const phase: RunsPhase =
    opened.failures > 0
      ? {kind: 'reconnecting', attempt: opened.failures, detail: opened.detail}
      : opened.ready
        ? {kind: 'live'}
        : {kind: 'connecting'};
  return {phase, runs: new Map(opened.runs)};
}

function notify(opened: HostRuns): void {
  const state = stateOf(opened);
  for (const listener of [...opened.listeners]) listener(state);
}

function alive(opened: HostRuns): number {
  let count = 0;
  for (const run of opened.runs.values()) {
    if (run.liveness === RunLiveness.ALIVE) count += 1;
  }
  return count;
}
