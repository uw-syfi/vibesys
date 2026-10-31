import {create} from '@bufbuild/protobuf';
import {type HostId, type HostKey, hostKey} from '@vibesys/hosts';
import {type RunHead, type RunLiveness, type TopicEntry, TopicEntrySchema} from '@vibesys/proto';
import {FakeClock} from '@vibesys/transport/testing';
import {type HostRuns, hostRuns} from '../shell/host-runs.js';
import {Subscriptions} from '../shell/subscriptions.js';
import {type FakeSubscribeStream, FakeSubscribeStreams} from './fake-subscribe-streams.js';

/** What a gateway announces about one run. */
export interface AnnouncedRun {
  readonly runId: string;
  readonly projectRoot: string;
  readonly liveness: RunLiveness;
  readonly head?: RunHead;
}

/** The `runs` entry a gateway sends for `run`. */
export function runEntry(run: AnnouncedRun): TopicEntry {
  const {runId, ...facts} = run;
  return create(TopicEntrySchema, {key: runId, value: {case: 'runs', value: facts}});
}

/**
 * Announce `runs` on each of `host`'s open subscriptions, then the end of the snapshot, as a
 * gateway does.
 */
export function announce(
  gateway: FakeSubscribeStreams,
  host: HostKey,
  runs: readonly AnnouncedRun[],
): void {
  announceEntries(gateway, host, runs.map(runEntry));
}

/** Announce entries as they are, then the end of the snapshot: for a frame a test builds itself. */
export function announceEntries(
  gateway: FakeSubscribeStreams,
  host: HostKey,
  entries: readonly TopicEntry[],
): void {
  for (const stream of gateway.open.filter(each => each.host === host)) {
    for (const entry of entries) stream.send({case: 'entry', value: entry});
    stream.send({case: 'synced', value: {}});
  }
}

/** Announce one change to a run, or its removal, on each of `host`'s open subscriptions. */
export function announceChange(
  gateway: FakeSubscribeStreams,
  host: HostKey,
  run: AnnouncedRun | {readonly runId: string; readonly removed: true},
): void {
  for (const stream of gateway.open.filter(each => each.host === host))
    stream.send(
      'removed' in run
        ? {case: 'removed', value: run.runId}
        : {case: 'entry', value: runEntry(run)},
    );
}

/** Host runs over a gateway that announces nothing, for tests that never look at a listing. */
export function idleHostRuns(): HostRuns {
  const clock = new FakeClock();
  return hostRuns(new Subscriptions(new FakeSubscribeStreams(), clock.schedule, () => clock.now()));
}

/**
 * A host's gateway as the app reads its runs: the generic fake topic behind the hub, and the
 * `HostRuns` over it. Hosts are named as the app names them, and a stream is the hub's open call.
 */
export class FakeRunsGateway {
  readonly streams = new FakeSubscribeStreams();
  readonly subscriptions: Subscriptions;
  readonly runs: HostRuns;

  constructor(readonly clock = new FakeClock()) {
    this.subscriptions = new Subscriptions(this.streams, clock.schedule, () => clock.now());
    this.runs = hostRuns(this.subscriptions);
  }

  /** The subscriptions the hub holds open for `host`. */
  open(host: HostId): FakeSubscribeStream[] {
    const key = hostKey(host);
    return this.streams.open.filter(stream => stream.host === key);
  }

  /** Announce `runs` on `host`, then the end of the snapshot, as its gateway does. */
  announce(host: HostId, runs: readonly AnnouncedRun[]): void {
    announce(this.streams, hostKey(host), runs);
  }

  /** Announce one change to a run on `host`, or its removal. */
  update(host: HostId, run: AnnouncedRun | {readonly runId: string; readonly removed: true}): void {
    announceChange(this.streams, hostKey(host), run);
  }
}
