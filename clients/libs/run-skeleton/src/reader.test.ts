import {expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {RunHeadSchema, RunLiveness, WatchRunsResponseSchema} from '@vibesys/proto';
import type {RunLivenessHint} from '@vibesys/run-skeleton';
import {FakeSkeletonCache} from '@vibesys/run-skeleton/cache';
import {gatewayReader} from '@vibesys/run-skeleton/reader';
import {FakeRunTransport} from '@vibesys/run-skeleton/testing';
import {FakeClock} from '@vibesys/transport/testing';

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
    const reader = gatewayReader(transport, new FakeSkeletonCache(), new FakeClock().schedule);
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
  const reader = gatewayReader(transport, new FakeSkeletonCache(), new FakeClock().schedule);
  await expect(
    reader.log('run-a', 'output', new AbortController().signal, () => {}),
  ).rejects.toThrow();
});

test('writer heads retain their canonical epoch even without process metadata', async () => {
  const transport = new FakeRunTransport({records: []});
  const head = create(RunHeadSchema, {epoch: 7, committedSeq: 2n, objective: {bestValue: 321}});
  transport.observe(
    create(WatchRunsResponseSchema, {runId: 'run-a', head, liveness: RunLiveness.INTERRUPTED}),
  );
  const reader = gatewayReader(transport, new FakeSkeletonCache(), new FakeClock().schedule);
  let stop = () => {};
  try {
    const hint = await new Promise<RunLivenessHint>(resolve => {
      stop = reader.watch('run-a', (_load, _failed, observed) => {
        if (observed?.head) resolve(observed);
      });
    });
    expect(hint.epoch).toBe(7);
    expect(hint.head).toEqual(head);
  } finally {
    stop();
  }
  expect(
    transport.calls
      .filter(call => !call.method.endsWith('Skeleton') && !call.method.endsWith('GetBlobs'))
      .every(call => call.signal.aborted),
  ).toBe(true);
});
