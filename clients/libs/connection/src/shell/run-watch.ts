/** One WatchRuns stream per host.

Cancelling aborts the stream. The gateway closes its watcher when that stream
ends. A late frame after cancel is ignored.
*/
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {type HostId, type HostKey, hostKey} from '@vibesys/hosts';
import {RunLiveness, WatchRunsRequestSchema, WatchRunsResponseSchema} from '@vibesys/proto';

export interface RunWatchPorts {
  stream(
    host: HostId,
    bytes: Uint8Array,
    signal: AbortSignal,
    receive: (bytes: Uint8Array) => void,
  ): Promise<void>;
}

export interface RunWatch {
  start(host: HostId): void;
  cancel(host: HostId): void;
  close(): void;
  live(host: HostId): Promise<number>;
}

interface HostRuns {
  readonly abort: AbortController;
  readonly runs: Map<string, RunLiveness>;
  ready: boolean;
  readonly waiters: ((count: number) => void)[];
}

/** Fold WatchRuns for the hosts this app is showing. `live` counts runs the host says are alive. */
export function runWatch(ports: RunWatchPorts): RunWatch {
  const hosts = new Map<HostKey, HostRuns>();
  return {
    start(host) {
      open(hosts, ports, host);
    },
    cancel(host) {
      stop(hosts, hostKey(host));
    },
    close() {
      for (const key of [...hosts.keys()]) stop(hosts, key);
    },
    live(host) {
      const opened = open(hosts, ports, host);
      if (opened.ready) return Promise.resolve(alive(opened));
      return new Promise(resolve => {
        opened.waiters.push(resolve);
      });
    },
  };
}

function open(hosts: Map<HostKey, HostRuns>, ports: RunWatchPorts, host: HostId): HostRuns {
  const key = hostKey(host);
  const existing = hosts.get(key);
  if (existing !== undefined) return existing;
  const opened: HostRuns = {
    abort: new AbortController(),
    runs: new Map(),
    ready: false,
    waiters: [],
  };
  hosts.set(key, opened);
  const bytes = toBinary(WatchRunsRequestSchema, create(WatchRunsRequestSchema, {}));
  void ports
    .stream(host, bytes, opened.abort.signal, frame => accept(opened, frame))
    .then(
      () => settle(opened),
      () => settle(opened),
    );
  return opened;
}

function accept(opened: HostRuns, frame: Uint8Array): void {
  if (opened.abort.signal.aborted) return;
  const message = fromBinary(WatchRunsResponseSchema, frame);
  if (message.initialComplete) {
    settle(opened);
    return;
  }
  if (message.removed) opened.runs.delete(message.runId);
  else opened.runs.set(message.runId, message.liveness);
}

function settle(opened: HostRuns): void {
  if (opened.ready) return;
  opened.ready = true;
  const count = alive(opened);
  for (const waiter of opened.waiters) waiter(count);
  opened.waiters.length = 0;
}

function stop(hosts: Map<HostKey, HostRuns>, key: HostKey): void {
  const opened = hosts.get(key);
  if (opened === undefined) return;
  hosts.delete(key);
  opened.abort.abort();
  settle(opened);
}

function alive(opened: HostRuns): number {
  let count = 0;
  for (const liveness of opened.runs.values()) {
    if (liveness === RunLiveness.ALIVE) count += 1;
  }
  return count;
}
