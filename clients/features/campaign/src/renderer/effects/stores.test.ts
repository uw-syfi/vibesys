import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {SimulatedClock} from 'xstate';
import {
  type CampaignFrame,
  type CampaignRecord,
  type CampaignStream,
  type FrameListener,
  framesFromRecord,
  parseReplayScenario,
  type StreamErrorListener,
} from '../../state/index.js';
import {
  createCampaignHistoryStore,
  createCampaignReplayStore,
  createLiveCampaignStore,
} from '../index.js';

const record = parseReplayScenario(
  JSON.parse(
    readFileSync(
      resolve(import.meta.dir, '../../../../../apps/web/dev/fixtures/trajectory-replay.json'),
      'utf8',
    ),
  ),
);

class FakeCampaignStream implements CampaignStream {
  #frame: FrameListener | null = null;
  #error: StreamErrorListener | null = null;
  releases = 0;
  subscribe(frame: FrameListener, error: StreamErrorListener): () => void {
    if (this.#frame !== null) throw new Error('stream supports one subscriber');
    this.#frame = frame;
    this.#error = error;
    return () => {
      this.#frame = null;
      this.#error = null;
      this.releases += 1;
    };
  }
  emit(frame: CampaignFrame): void {
    this.#frame?.(frame);
  }
  fail(): void {
    this.#error?.(new Error('stream unreadable'));
  }
}

async function flush(): Promise<void> {
  for (let turn = 0; turn < 10; turn++) await Promise.resolve();
}

test('history preserves navigation and inspection, plays on injected clock, and stops at the tail', () => {
  const clock = new SimulatedClock();
  const store = createCampaignHistoryStore(record, {clock});
  expect(store.getState().campaign?.pointIndex).toBe(record.measurements.length - 1);
  store.getState().campaign?.setPointIndex(10);
  store.getState().campaign?.setSelectedWorkstreamId(record.workstreams[0]?.id ?? null);
  expect(store.getState().campaign?.pointIndex).toBe(10);
  store.getState().campaign?.setPlaying(true);
  clock.increment(300);
  expect(store.getState().campaign?.pointIndex).toBe(11);
  store.getState().campaign?.setPointIndex(record.measurements.length - 2);
  store.getState().campaign?.setPlaying(true);
  clock.increment(300);
  expect(store.getState().campaign?.pointIndex).toBe(record.measurements.length - 1);
  expect(store.getState().campaign?.playing).toBe(false);
  clock.increment(3000);
  expect(store.getState().campaign?.pointIndex).toBe(record.measurements.length - 1);
  store.close();
});

test('live follows frames, pins a scrub, recovers errors, and keeps completion independent of cursor', () => {
  const stream = new FakeCampaignStream();
  const store = createLiveCampaignStore(stream);
  const frames = framesFromRecord(record);
  for (const frame of frames) stream.emit(frame);
  expect(store.getState().campaign?.pointIndex).toBe(record.measurements.length - 1);
  expect(store.getState().campaign?.status).toBe('completed');
  expect(store.getState().campaign?.workstreamTokenSpend).toEqual({});
  store.getState().campaign?.setPointIndex(12);
  expect(store.getState().campaign?.status).toBe('completed');
  expect(store.getState().campaign?.playing).toBe(false);
  stream.fail();
  expect(store.getState().error).toBe('stream unreadable');
  for (const frame of frames) stream.emit(frame);
  expect(store.getState().error).toBeNull();
  expect(store.getState().scenario?.measurements.length).toBe(record.measurements.length);
  expect(store.getState().campaign?.pointIndex).toBe(12);
  store.getState().campaign?.setPlaying(true);
  expect(store.getState().campaign?.pointIndex).toBe(record.measurements.length - 1);
  store.close();
  store.close();
  expect(stream.releases).toBe(1);
});

test('a growing stream follows until explicitly paused and ignores events after close', () => {
  const stream = new FakeCampaignStream();
  const store = createLiveCampaignStore(stream);
  const frames = framesFromRecord(record);
  const prefix = frames.findIndex(
    frame => frame.kind === 'measurement' && frame.measurement.sequence === 20,
  );
  for (const frame of frames.slice(0, prefix + 1)) stream.emit(frame);
  expect(store.getState().campaign?.pointIndex).toBe(19);
  store.getState().campaign?.setPlaying(false);
  for (const frame of frames.slice(prefix + 1)) stream.emit(frame);
  expect(store.getState().campaign?.pointIndex).toBe(19);
  store.close();
  const closed = store.getState();
  for (const frame of frames) stream.emit(frame);
  expect(store.getState()).toBe(closed);
});

test('replay retries a failed load, then validates through the app-injected loader', async () => {
  let attempts = 0;
  const store = createCampaignReplayStore(async () => {
    attempts++;
    if (attempts === 1) throw new Error('fixture unavailable');
    return record;
  });
  store.dispatch({type: 'load'});
  await flush();
  expect(store.getState().error).toBe('fixture unavailable');
  expect(store.getState().loading).toBe(false);
  store.dispatch({type: 'load'});
  await flush();
  expect(store.getState().error).toBeNull();
  expect(store.getState().scenario?.id).toBe(record.id);
  store.close();
});

test('closing aborts the unfinished load and a late reply cannot publish another campaign', async () => {
  let signal: AbortSignal | undefined;
  let resolveLoad: ((record: CampaignRecord) => void) | undefined;
  const store = createCampaignReplayStore(input => {
    signal = input;
    return new Promise(resolve => {
      resolveLoad = resolve;
    });
  });
  store.dispatch({type: 'load'});
  store.close();
  expect(signal?.aborted).toBe(true);
  const closed = store.getState();
  resolveLoad?.(record);
  await flush();
  expect(store.getState()).toBe(closed);
  expect(store.getState().scenario).toBeNull();
});
