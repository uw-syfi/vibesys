import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {gunzipSync} from 'node:zlib';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  RecordSchema,
  RunHeadSchema,
  RunLiveness,
  SkeletonRequestSchema,
  SkeletonResponseSchema,
  TailRequestSchema,
  TailResponseSchema,
} from '@vibesys/proto';
import {foldSkeleton, observeRun, type RunLivenessHint, seedSkeleton} from '@vibesys/run-skeleton';
import {FakeSkeletonCache} from '@vibesys/run-skeleton/cache';
import {type GatewayObservation, gatewayReader} from '@vibesys/run-skeleton/reader';
import {FakeRunTransport} from '@vibesys/run-skeleton/testing';
import {FakeClock} from '@vibesys/transport/testing';
import fc from 'fast-check';

const METHOD = '/vibesys.api.v1.HostService/TailStream';

test('UTF-8 text offsets cover decoded bytes across every possible codepoint split', async () => {
  const text = 'aé中😀\nlast';
  const encoder = new TextEncoder();
  const data = encoder.encode(text);
  for (let size = 1; size <= data.length; size += 1) {
    const transport = new FakeRunTransport({
      records: [],
      streams: {output: data},
      streamChunkBytes: size,
    });
    transport.close(METHOD);
    const reader = gatewayReader(
      transport,
      new FakeSkeletonCache(),
      new FakeClock().schedule,
      transport.host.feed,
    );
    const chunks: {text: string; offset: number; next: number}[] = [];
    await reader.log('run-a', 'output', new AbortController().signal, chunk => chunks.push(chunk));
    expect(chunks.map(chunk => chunk.text).join('')).toBe(text);
    let offset = 0;
    for (const chunk of chunks) {
      expect(chunk.offset).toBe(offset);
      expect(chunk.next - chunk.offset).toBe(encoder.encode(chunk.text).length);
      offset = chunk.next;
    }
    expect(offset).toBe(data.length);
  }
});

test('a finite UTF-8 stream rejects a torn final codepoint instead of silently losing it', async () => {
  const transport = new FakeRunTransport({
    records: [],
    streams: {output: new Uint8Array([0xf0, 0x9f])},
  });
  transport.close(METHOD);
  const reader = gatewayReader(
    transport,
    new FakeSkeletonCache(),
    new FakeClock().schedule,
    transport.host.feed,
  );
  await expect(
    reader.log('run-a', 'output', new AbortController().signal, () => {}),
  ).rejects.toThrow();
});

for (const epoch of [1, 7, 4294967295])
  for (const format of ['canonical', 'process', 'both', 'conflicting', 'absent'] as const)
    test(`released writer observations preserve the epoch fence (${epoch}, ${format})`, async () => {
      const transport = new FakeRunTransport({records: []});
      const head = create(RunHeadSchema, {
        ...(format === 'process' || format === 'absent' ? {} : {epoch}),
        ...(format === 'process' || format === 'both' || format === 'conflicting'
          ? {process: {epoch: format === 'conflicting' ? epoch - 1 : epoch}}
          : {}),
        committedSeq: 2n,
        objective: {bestValue: 321},
      });
      transport.observe({runId: 'run-a', head, liveness: RunLiveness.INTERRUPTED});
      const reader = gatewayReader(
        transport,
        new FakeSkeletonCache(),
        new FakeClock().schedule,
        transport.host.feed,
      );
      let stop = () => {};
      try {
        const hint = await new Promise<RunLivenessHint>(resolve => {
          stop = reader.watch(
            'run-a',
            ({hint: observed}) => {
              if (observed?.head) resolve(observed);
            },
            () => {},
          );
        });
        expect(hint.epoch).toBe(format === 'absent' || format === 'process' ? 0 : epoch);
        expect(hint.head).toEqual(head);
        for (const paused of [false, true]) {
          const skeleton = foldSkeleton(seedSkeleton(), [
            create(RecordSchema, {
              seq: 1n,
              epoch,
              body: {case: 'epochStarted', value: {runId: 'run-a', epoch}},
            }),
            create(RecordSchema, {
              seq: 2n,
              epoch,
              body: {
                case: 'lifecycle',
                value: {
                  state: {
                    case: 'running',
                    value: paused ? {hold: {reason: {case: 'operator', value: {}}}} : {},
                  },
                },
              },
            }),
          ]);
          expect(observeRun(skeleton, {...hint, value: RunLiveness.ALIVE}).interrupted).toBe(false);
          expect(observeRun(skeleton, hint).interrupted).toBe(
            format !== 'absent' && format !== 'process',
          );
          expect(observeRun(skeleton, {...hint, epoch: epoch - 1}).interrupted).toBe(false);
          expect(observeRun(skeleton, {...hint, epoch: epoch + 1}).interrupted).toBe(false);
          expect(observeRun(skeleton).lifecycle?.state.case).toBe('running');
        }
      } finally {
        stop();
      }
      expect(
        transport.calls
          .filter(call => !call.method.endsWith('Skeleton') && !call.method.endsWith('GetBlobs'))
          .every(call => call.signal.aborted),
      ).toBe(true);
    });

test('UTF-8 tails preserve absolute byte offsets when reopened at any decoded boundary', async () => {
  const text = 'aé中😀\nlast';
  const data = new TextEncoder().encode(text);
  let offset = 0;
  for (const character of text) {
    const transport = new FakeRunTransport({
      records: [],
      streams: {output: data},
      streamChunkBytes: 1,
    });
    transport.close(METHOD);
    const reader = gatewayReader(
      transport,
      new FakeSkeletonCache(),
      new FakeClock().schedule,
      transport.host.feed,
    );
    const chunks: {text: string; offset: number; next: number}[] = [];
    await reader.log(
      'run-a',
      'output',
      new AbortController().signal,
      chunk => chunks.push(chunk),
      BigInt(offset),
    );
    expect(chunks[0]?.offset).toBe(offset);
    expect(chunks.map(chunk => chunk.text).join('')).toBe(
      new TextDecoder().decode(data.slice(offset)),
    );
    expect(chunks.at(-1)?.next).toBe(data.length);
    offset += new TextEncoder().encode(character).length;
  }
});

test('the retained conversion corpus obeys gateway byte bounds and continues through Tail', async () => {
  const root = new URL('../../../../proto/testdata/conversion/', import.meta.url);
  const values: string[] = JSON.parse(
    gunzipSync(readFileSync(new URL('records.base64.json.gz', root))).toString(),
  );
  const records = values.map(value =>
    fromBinary(RecordSchema, Uint8Array.from(Buffer.from(value, 'base64'))),
  );
  const transport = new FakeRunTransport({records});
  const abort = new AbortController();
  const first = await transport.unary(
    '/vibesys.api.v1.HostService/Skeleton',
    toBinary(SkeletonRequestSchema, create(SkeletonRequestSchema, {runId: 'run-1'})),
    abort.signal,
  );
  expect(first.byteLength).toBeLessThanOrEqual(1 << 20);
  const prefix = fromBinary(SkeletonResponseSchema, first);
  expect(prefix.complete).toBe(false);
  expect(prefix.records.length).toBeGreaterThan(0);
  let count = 0;
  for await (const bytes of transport.stream(
    '/vibesys.api.v1.HostService/Tail',
    toBinary(TailRequestSchema, create(TailRequestSchema, {runId: 'run-1', fromSeq: 1n})),
    abort.signal,
  )) {
    expect(bytes.byteLength).toBeLessThanOrEqual(1 << 20);
    const response = fromBinary(TailResponseSchema, bytes);
    count += response.records.length;
    if (response.throughSeq === records.at(-1)?.seq) abort.abort();
  }
  expect(count).toBe(records.length);
  expect(transport.activeStreams).toBe(0);
});

test('property: the host losing its stream makes the reader forget its last word, and restoring it brings the word back', async () => {
  await fc.assert(
    fc.asyncProperty(
      fc.integer({min: 1, max: 50}),
      fc.array(fc.boolean(), {minLength: 1, maxLength: 6}),
      async (start, losses) => {
        const transport = new FakeRunTransport({records: []});
        const clock = new FakeClock();
        const reader = gatewayReader(
          transport,
          new FakeSkeletonCache(),
          clock.schedule,
          transport.host.feed,
        );
        let latest: GatewayObservation | undefined;
        const stop = reader.watch(
          'run-a',
          observation => {
            latest = observation;
          },
          () => {},
        );
        try {
          transport.observe({runId: 'run-a', liveness: RunLiveness.ALIVE, head: {epoch: start}});
          await transport.host.watched();
          expect(latest?.hint?.epoch).toBe(start);
          expect(latest?.fresh).toBe(true);
          for (const lose of losses) {
            if (lose) transport.host.lose();
            // A lost host's last word is no fact.
            expect(latest?.fresh).toBe(!lose);
            expect(latest?.hint === undefined).toBe(lose);
            transport.host.restore();
            expect(latest?.fresh).toBe(true);
            expect(latest?.hint?.epoch).toBe(start);
          }
        } finally {
          stop();
        }
        // The reader holds no timer of its own for liveness: the host's subscription holds the lease.
        expect(clock.pending).toBe(0);
        expect(transport.host.watching).toBe(0);
      },
    ),
    {numRuns: 12, seed: 34371},
  );
});

test('property: a lock probe of a successor epoch is unconfirmed until its writer was seen alive', async () => {
  const frames = fc.array(
    fc.record({
      epoch: fc.integer({min: 0, max: 2}),
      liveness: fc.constantFrom(
        RunLiveness.ALIVE,
        RunLiveness.INTERRUPTED,
        RunLiveness.ENDED,
        RunLiveness.UNSPECIFIED,
      ),
    }),
    {minLength: 1, maxLength: 6},
  );
  await fc.assert(
    fc.asyncProperty(fc.integer({min: 1, max: 5}), frames, async (base, steps) => {
      const transport = new FakeRunTransport({records: []});
      const clock = new FakeClock();
      const reader = gatewayReader(
        transport,
        new FakeSkeletonCache(),
        clock.schedule,
        transport.host.feed,
      );
      let latest: GatewayObservation | undefined;
      let notify = () => {};
      const stop = reader.watch(
        'run-a',
        observation => {
          latest = observation;
          notify();
        },
        () => {},
      );
      try {
        // The predecessor's end is the one closed fact; later frames follow it.
        const sequence = [{epoch: 0, liveness: RunLiveness.ENDED}, ...steps];
        let alive = 0;
        for (const [index, step] of sequence.entries()) {
          const epoch = base + step.epoch;
          const received = Promise.withResolvers<void>();
          notify = () => {
            if (latest?.fresh && latest.hint?.head?.committedSeq === BigInt(index))
              received.resolve();
          };
          transport.observe({
            runId: 'run-a',
            liveness: step.liveness,
            head: {epoch, committedSeq: BigInt(index)},
          });
          await received.promise;
          if (step.liveness === RunLiveness.ALIVE) alive = Math.max(alive, epoch);
          const pending =
            step.liveness === RunLiveness.INTERRUPTED && epoch > base && alive < epoch;
          expect(latest?.hint?.value).toBe(pending ? RunLiveness.UNSPECIFIED : step.liveness);
        }
        transport.host.lose();
        expect(latest?.fresh).toBe(false);
        expect(latest?.hint).toBeUndefined();
      } finally {
        stop();
      }
    }),
    {numRuns: 20, seed: 341812},
  );
});
