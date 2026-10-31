/** Saved-run metadata discovery: one stream per project, one scan on the host. */
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {timestampDate} from '@bufbuild/protobuf/wkt';
import type {HostId, ListedRun} from '@vibesys/hosts';
import {
  decodeHostWatch,
  encodeHostWatch,
  SavedRunImport,
  StreamSavedRunsRequestSchema,
  type StreamSavedRunsResponse,
  StreamSavedRunsResponseSchema,
} from '@vibesys/proto';
import {newRequestId} from '@vibesys/request-identity';
import {requireCheckout} from './registry-reads.js';

export interface SavedRunsPorts {
  checkout(host: HostId): Promise<string | null>;
  stream(
    host: HostId,
    bytes: Uint8Array,
    signal: AbortSignal,
    receive: (bytes: Uint8Array) => void,
  ): Promise<void>;
}

export interface RegistrySavedRuns {
  /**
   * Every run of `project`, newest first. A listing that ends any other way than its completing
   * frame fails; once a frame has arrived the failure is a `SavedRunsInterrupted`, which keeps the
   * rows received.
   */
  list(host: HostId, project: string, signal: AbortSignal): Promise<readonly ListedRun[]>;
}

/**
 * A listing that failed or ended after some of its rows arrived. It words itself as its cause, so
 * a screen that shows an error's message shows the same words as for a listing that gave nothing.
 */
export class SavedRunsInterrupted extends Error {
  readonly runs: readonly ListedRun[];
  constructor(runs: readonly ListedRun[], cause: unknown) {
    super(cause instanceof Error ? cause.message : 'The project could not be listed', {cause});
    this.name = 'SavedRunsInterrupted';
    this.runs = runs;
  }
}

function newestFirst(runs: Iterable<ListedRun>): ListedRun[] {
  return [...runs].sort(
    (left, right) => right.createdAt - left.createdAt || right.runId.localeCompare(left.runId),
  );
}

function listedRun(run: StreamSavedRunsResponse['runs'][number]): ListedRun {
  if (!run.createdAt) throw new Error('saved run has no creation timestamp');
  return {
    runId: run.runId,
    name: run.displayName,
    task: run.task ?? null,
    createdAt: timestampDate(run.createdAt).getTime(),
    ...(run.imported === SavedRunImport.UNAVAILABLE ? {unavailable: run.detail} : {}),
  };
}

/** What a listing holds so far: each run once, and whether its completing frame came. */
class Received {
  readonly runs = new Map<string, ListedRun>();
  frames = 0;
  complete = false;

  constructor(private readonly requestId: string) {}

  accept(payload: Uint8Array): void {
    const frame = fromBinary(
      StreamSavedRunsResponseSchema,
      decodeHostWatch('StreamSavedRuns', payload),
    );
    if (frame.requestId !== this.requestId) throw new Error('saved-run listing identity mismatch');
    if (this.complete) throw new Error('saved-run listing continued after it was complete');
    for (const run of frame.runs) this.runs.set(run.runId, listedRun(run));
    this.frames += 1;
    this.complete = frame.complete;
  }
}

export function registrySavedRuns(ports: SavedRunsPorts): RegistrySavedRuns {
  return {
    async list(host, project, signal) {
      const checkout = requireCheckout(await ports.checkout(host), null);
      const requestId = newRequestId();
      const bytes = encodeHostWatch(
        checkout,
        'StreamSavedRuns',
        toBinary(
          StreamSavedRunsRequestSchema,
          create(StreamSavedRunsRequestSchema, {requestId, project}),
        ),
      );
      const received = new Received(requestId);
      try {
        await ports.stream(host, bytes, signal, payload => received.accept(payload));
        if (!received.complete) throw new Error('saved-run listing ended before it was complete');
      } catch (error) {
        if (received.frames === 0) throw error;
        throw new SavedRunsInterrupted(newestFirst(received.runs.values()), error);
      }
      return newestFirst(received.runs.values());
    },
  };
}
