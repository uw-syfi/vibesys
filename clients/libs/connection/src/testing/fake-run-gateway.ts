import {create, toBinary} from '@bufbuild/protobuf';
import {type HostId, type HostKey, hostKey} from '@vibesys/hosts';
import {type RunHead, type RunLiveness, WatchRunsResponseSchema} from '@vibesys/proto';
import {FakeClock} from '@vibesys/transport/testing';
import {type RunWatch, type RunWatchPorts, runWatch} from '../shell/run-watch.js';

/** One WatchRuns stream a host's gateway holds open, with the means to speak and end it. */
export interface FakeRunStream {
  readonly host: HostKey;
  readonly signal: AbortSignal;
  receive(bytes: Uint8Array): void;
  end(): void;
  fail(error: Error): void;
}

/** What a gateway announces about one run. */
export interface AnnouncedRun {
  readonly runId: string;
  readonly projectRoot: string;
  readonly liveness: RunLiveness;
  readonly head?: RunHead;
}

/** A host's gateway as `runWatch` sees it: frames and endings are the test's explicit inputs. */
export class FakeRunGateway implements RunWatchPorts {
  readonly streams: FakeRunStream[] = [];

  stream(
    host: HostId,
    _bytes: Uint8Array,
    signal: AbortSignal,
    receive: (bytes: Uint8Array) => void,
  ): Promise<void> {
    return new Promise((end, fail) => {
      this.streams.push({host: hostKey(host), signal, receive, end, fail});
    });
  }

  /** The streams of `host` that are still wanted. */
  open(host: HostId): FakeRunStream[] {
    const key = hostKey(host);
    return this.streams.filter(stream => stream.host === key && !stream.signal.aborted);
  }

  /** Announce `runs` on `host`'s open stream, then the initial marker, as a gateway does. */
  announce(host: HostId, runs: readonly AnnouncedRun[]): void {
    for (const stream of this.open(host)) {
      for (const run of runs)
        stream.receive(
          toBinary(WatchRunsResponseSchema, create(WatchRunsResponseSchema, {...run})),
        );
      stream.receive(
        toBinary(WatchRunsResponseSchema, create(WatchRunsResponseSchema, {initialComplete: true})),
      );
    }
  }

  /** Announce one change to a run on `host`'s open stream. */
  update(host: HostId, run: AnnouncedRun | {readonly runId: string; readonly removed: true}): void {
    for (const stream of this.open(host))
      stream.receive(toBinary(WatchRunsResponseSchema, create(WatchRunsResponseSchema, {...run})));
  }
}

/** A run watch over a gateway that announces nothing, for tests that never look at a listing. */
export function idleRunWatch(): RunWatch {
  return runWatch(new FakeRunGateway(), new FakeClock().schedule);
}
