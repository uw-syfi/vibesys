import {expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import type {HostId, ListedRun} from '@vibesys/hosts';
import {
  FailureCategory,
  OperationFailed,
  SavedRunImport,
  WatchRequestSchema,
  WatchResponseSchema,
} from '@vibesys/proto';
import fc from 'fast-check';
import {registrySavedRuns, SavedRunsInterrupted} from './registry-saved-runs.js';

const HOST: HostId = {kind: 'local'};

interface Row {
  readonly runId: string;
  readonly seconds: number;
  readonly unavailable: boolean;
}

/** What the host sends: frames of rows, then how the stream ends. */
interface Script {
  readonly frames: readonly (readonly Row[])[];
  readonly end:
    | 'complete'
    | 'silent'
    | 'failure'
    | 'break'
    | 'complete-then-failure'
    | 'complete-then-break';
}

function row(source: Row) {
  return {
    runId: source.runId,
    displayName: `Saved ${source.runId}`,
    task: 'task-a',
    createdAt: {seconds: BigInt(source.seconds)},
    imported: source.unavailable ? SavedRunImport.UNAVAILABLE : SavedRunImport.AVAILABLE,
    detail: source.unavailable ? 'old format' : '',
  };
}

/**
 * The host's stream, in memory: the registry still encodes the request and decodes and correlates
 * every frame as in production. `end` says how the script finishes after its last frame.
 */
function host(script: Script) {
  const sent: string[] = [];
  const streams: AbortSignal[] = [];
  const completing = script.end === 'complete' || script.end.startsWith('complete-then');
  const reads = registrySavedRuns({
    async checkout() {
      return '/checkout';
    },
    async stream(_host, bytes, signal, receive) {
      if (signal.aborted) throw new Error('cancelled');
      const request = fromBinary(WatchRequestSchema, bytes);
      if (request.method.case !== 'streamSavedRuns') throw new Error('unexpected method');
      const {requestId, project} = request.method.value;
      sent.push(project);
      streams.push(signal);
      const frame = (runs: readonly Row[], complete: boolean) =>
        toBinary(
          WatchResponseSchema,
          create(WatchResponseSchema, {
            result: {
              case: 'streamSavedRuns',
              value: {requestId, projectRoot: project, runs: runs.map(row), complete},
            },
          }),
        );
      script.frames.forEach((runs, index) => {
        const last = index === script.frames.length - 1;
        receive(frame(runs, last && completing));
      });
      if (script.frames.length === 0 && completing) receive(frame([], true));
      if (script.end === 'failure' || script.end === 'complete-then-failure') {
        receive(
          toBinary(
            WatchResponseSchema,
            create(WatchResponseSchema, {
              result: {
                case: 'failure',
                value: {category: FailureCategory.DEADLINE_EXCEEDED, message: 'silent'},
              },
            }),
          ),
        );
      }
      if (script.end === 'break' || script.end === 'complete-then-break')
        throw new Error('gateway closed');
    },
  });
  return {reads, sent, streams};
}

const listed = (reads: ReturnType<typeof host>['reads']) =>
  reads.list(HOST, '/project', new AbortController().signal);

function expected(frames: readonly (readonly Row[])[]): ListedRun[] {
  const byId = new Map<string, ListedRun>();
  for (const source of frames.flat())
    byId.set(source.runId, {
      runId: source.runId,
      name: `Saved ${source.runId}`,
      task: 'task-a',
      createdAt: source.seconds * 1000,
      ...(source.unavailable ? {unavailable: 'old format'} : {}),
    });
  return [...byId.values()].sort(
    (left, right) => right.createdAt - left.createdAt || right.runId.localeCompare(left.runId),
  );
}

const rows = fc.uniqueArray(
  fc.record({
    runId: fc.stringMatching(/^[a-z0-9-]{1,12}$/),
    seconds: fc.integer({min: 1, max: 4_000_000_000}),
    unavailable: fc.boolean(),
  }),
  {selector: source => source.runId, maxLength: 30},
);

/** Any cut of the rows into frames, however the host batches them. */
const frames = rows.chain(all =>
  fc.array(fc.integer({min: 0, max: Math.max(all.length, 1)}), {maxLength: 6}).map(cuts => {
    const bounds = [0, ...[...cuts].sort((a, b) => a - b), all.length];
    return bounds.slice(1).map((end, index) => all.slice(bounds[index], end));
  }),
);

test('a listing keeps its metadata and import refusal reasons, newest first', async () => {
  const sources: Row[][] = [
    [{runId: 'run-0', seconds: 100, unavailable: false}],
    [{runId: 'run-1', seconds: 101, unavailable: true}],
  ];
  const {reads, sent} = host({frames: sources, end: 'complete'});
  expect(await listed(reads)).toEqual([
    {
      runId: 'run-1',
      name: 'Saved run-1',
      task: 'task-a',
      createdAt: 101000,
      unavailable: 'old format',
    },
    {runId: 'run-0', name: 'Saved run-0', task: 'task-a', createdAt: 100000},
  ]);
  expect(sent).toEqual(['/project']);
});

test('however the host cuts its frames, a completed listing holds every run once', async () => {
  await fc.assert(
    fc.asyncProperty(frames, async cut => {
      const {reads} = host({frames: cut, end: 'complete'});
      expect(await listed(reads)).toEqual(expected(cut));
    }),
  );
});

test('the completing frame ends the listing: a fault after it changes nothing and the stream is cancelled', async () => {
  const ends = fc.constantFrom('complete-then-failure', 'complete-then-break') as fc.Arbitrary<
    Script['end']
  >;
  await fc.assert(
    fc.asyncProperty(frames, ends, async (cut, end) => {
      const {reads, streams} = host({frames: cut, end});
      expect(await listed(reads)).toEqual(expected(cut));
      expect(streams.map(stream => stream.aborted)).toEqual([true]);
    }),
  );
});

test('a listing that stops after some frames fails with every row received so far', async () => {
  const ends = fc.constantFrom('silent', 'failure', 'break') as fc.Arbitrary<Script['end']>;
  await fc.assert(
    fc.asyncProperty(frames, ends, async (cut, end) => {
      const {reads} = host({frames: cut, end});
      const error = await listed(reads).then(
        () => undefined,
        (failure: unknown) => failure,
      );
      expect(error).toBeInstanceOf(SavedRunsInterrupted);
      expect((error as SavedRunsInterrupted).runs).toEqual(expected(cut));
    }),
  );
});

test("a failure before any frame is the call's own failure, not a partial listing", async () => {
  const error = await listed(host({frames: [], end: 'failure'}).reads).catch(
    (failure: unknown) => failure,
  );
  expect(error).toBeInstanceOf(OperationFailed);
  expect(error).not.toBeInstanceOf(SavedRunsInterrupted);
});

test('an interrupted listing is worded as its cause, so no screen text changes', async () => {
  const sources = [[{runId: 'run-0', seconds: 100, unavailable: false}]];
  const failed = await listed(host({frames: sources, end: 'failure'}).reads).catch(
    (error: unknown) => error,
  );
  const first = await listed(host({frames: [], end: 'failure'}).reads).catch(
    (error: unknown) => error,
  );
  expect(first).toBeInstanceOf(OperationFailed);
  expect((failed as SavedRunsInterrupted).message).toBe((first as OperationFailed).message);
  expect((failed as SavedRunsInterrupted).cause).toBeInstanceOf(OperationFailed);
});

test('a stream that ends without its completing frame is a failure, not a short list', async () => {
  const sources = [[{runId: 'run-0', seconds: 100, unavailable: false}]];
  const error = await listed(host({frames: sources, end: 'silent'}).reads).catch(
    (failure: unknown) => failure,
  );
  expect(error).toBeInstanceOf(SavedRunsInterrupted);
  expect((error as SavedRunsInterrupted).message).toContain('before it was complete');
});

test('a cancelled listing asks nothing of the host', async () => {
  const {reads, sent} = host({frames: [], end: 'complete'});
  const controller = new AbortController();
  controller.abort();
  await expect(reads.list(HOST, '/project', controller.signal)).rejects.toThrow('cancelled');
  expect(sent).toEqual([]);
});
