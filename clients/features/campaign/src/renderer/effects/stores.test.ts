import {expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {resolve} from 'node:path';
import {SimulatedClock} from 'xstate';
import {
  type CampaignFrame,
  type CampaignHeader,
  type CampaignRecord,
  type CampaignStream,
  type FrameListener,
  type FrameMeasurement,
  type FrameWorkstream,
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

const liveHeader: CampaignHeader = {
  id: record.id,
  title: record.title,
  summary: record.summary,
  provenance: record.provenance,
  objective: record.objective,
  benchmarkVersions: record.benchmarkVersions,
  benchmarkVersionBoundary: record.benchmarkVersionBoundary,
  trajectories: [],
};

function liveMeasurement(id: string, sequence: number, timestamp: string): FrameMeasurement {
  const source = record.measurements[0];
  if (source === undefined) throw new Error('campaign fixture must contain a measurement');
  return {...source, id, sequence, timestamp};
}

function liveWorkstream(overrides: Partial<FrameWorkstream> = {}): FrameWorkstream {
  return {
    id: 'live-workstream',
    title: 'Live workstream',
    hypothesis: 'Test the live observation horizon',
    startedAt: '2026-01-01T00:00:00Z',
    firstSequence: 1,
    phase: 'implementing',
    active: true,
    finishedAt: null,
    lastSequence: null,
    outcome: null,
    outcomeSummary: '',
    ...overrides,
  };
}

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

test('live follows frames, pins a scrub, recovers transport errors, and releases on completion', () => {
  const stream = new FakeCampaignStream();
  const store = createLiveCampaignStore(stream);
  const frames = framesFromRecord(record);
  const completion = frames.at(-1);
  expect(completion?.kind).toBe('status');
  for (const frame of frames.slice(0, -1)) stream.emit(frame);
  expect(store.getState().campaign?.pointIndex).toBe(record.measurements.length - 1);
  expect(store.getState().campaign?.status).toBe('active');
  expect(store.getState().campaign?.workstreamTokenSpend).toEqual({});
  store.getState().campaign?.setPointIndex(12);
  expect(store.getState().campaign?.playing).toBe(false);
  stream.fail();
  expect(store.getState().error).toBe('stream unreadable');
  const replayedInit = frames[0];
  if (replayedInit === undefined) throw new Error('fixture must contain an init frame');
  stream.emit(replayedInit);
  expect(store.getState().error).toBe('stream unreadable');
  for (const frame of frames) stream.emit(frame);
  expect(store.getState().error).toBeNull();
  expect(store.getState().campaign?.status).toBe('completed');
  expect(store.getState().scenario?.measurements.length).toBe(record.measurements.length);
  expect(store.getState().campaign?.pointIndex).toBe(12);
  store.getState().campaign?.setPlaying(true);
  expect(store.getState().campaign?.pointIndex).toBe(record.measurements.length - 1);
  expect(stream.releases).toBe(1);
  stream.fail();
  expect(store.getState().error).toBeNull();
  store.close();
  store.close();
  expect(stream.releases).toBe(1);
});

test('a sequence gap latches the first integrity error and releases the corrupt stream', () => {
  const stream = new FakeCampaignStream();
  const store = createLiveCampaignStore(stream);
  const [init] = framesFromRecord(record);
  expect(init?.kind).toBe('campaign-init');
  if (init === undefined) throw new Error('expected campaign init frame');
  stream.emit(init);
  const beforeGap = store.getState().scenario;

  stream.emit({kind: 'tokens', seq: 3, workstreamId: 'ws-1', tokens: 30});
  expect(store.getState().error).toBe('campaign stream sequence gap: expected 2, received 3');
  expect(store.getState().scenario).toEqual(beforeGap);
  expect(stream.releases).toBe(1);

  // A later expected frame cannot prove that the stream is authoritative
  // again. The failed subscription is closed, so neither it nor subsequent
  // frames can mutate data or clear the first integrity failure.
  const firstAgent = record.agents[0];
  if (firstAgent === undefined) throw new Error('fixture must contain an agent');
  stream.emit({kind: 'agent-upsert', seq: 2, agent: firstAgent});
  stream.emit({kind: 'tokens', seq: 3, workstreamId: 'ws-1', tokens: 30});
  expect(store.getState().error).toBe('campaign stream sequence gap: expected 2, received 3');
  expect(store.getState().scenario).toEqual(beforeGap);
  expect(store.getState().scenario?.agents).toEqual([]);
  store.close();
  expect(stream.releases).toBe(1);
});

test('conflicting sequence reuse latches an error and releases the stream', () => {
  const stream = new FakeCampaignStream();
  const store = createLiveCampaignStore(stream);
  const [init] = framesFromRecord(record);
  if (init === undefined) throw new Error('fixture must contain an init frame');
  stream.emit(init);
  stream.emit({kind: 'tokens', seq: 2, workstreamId: 'ws-1', tokens: 100});
  expect(store.getState().campaign?.workstreamTokenSpend).toEqual({'ws-1': 100});

  stream.emit({kind: 'tokens', seq: 2, workstreamId: 'ws-1', tokens: 101});
  const failure =
    'campaign stream sequence 2 conflicts with its observed tokens frame; received tokens';
  expect(store.getState().error).toBe(failure);
  expect(store.getState().campaign?.workstreamTokenSpend).toEqual({'ws-1': 100});
  expect(stream.releases).toBe(1);

  stream.emit({kind: 'tokens', seq: 3, workstreamId: 'ws-1', tokens: 300});
  expect(store.getState().error).toBe(failure);
  expect(store.getState().campaign?.workstreamTokenSpend).toEqual({'ws-1': 100});
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

test('live follow observes a workstream starting after the last measurement while scrub stays historical', () => {
  const stream = new FakeCampaignStream();
  const store = createLiveCampaignStore(stream);
  stream.emit({kind: 'campaign-init', seq: 1, header: liveHeader});
  stream.emit({
    kind: 'measurement',
    seq: 2,
    measurement: liveMeasurement('early', 1, '2026-01-01T00:00:05Z'),
  });
  stream.emit({
    kind: 'measurement',
    seq: 3,
    measurement: liveMeasurement('latest', 2, '2026-01-01T00:00:10Z'),
  });
  stream.emit({
    kind: 'workstream-upsert',
    seq: 4,
    workstream: liveWorkstream({startedAt: '2026-01-01T00:00:30Z'}),
  });

  expect(store.getState().campaign?.latestTimestamp).toBe('2026-01-01T00:00:30.000Z');
  expect(store.getState().campaign?.visibleWorkstreamCount).toBe(1);

  store.getState().campaign?.setPointIndex(0);
  expect(store.getState().campaign?.latestTimestamp).toBe('2026-01-01T00:00:05Z');
  expect(store.getState().campaign?.visibleWorkstreamCount).toBe(0);
  store.close();
});

test('live follow observes a workstream settling after the last measurement', () => {
  const stream = new FakeCampaignStream();
  const store = createLiveCampaignStore(stream);
  stream.emit({kind: 'campaign-init', seq: 1, header: liveHeader});
  stream.emit({kind: 'workstream-upsert', seq: 2, workstream: liveWorkstream()});
  stream.emit({
    kind: 'measurement',
    seq: 3,
    measurement: liveMeasurement('only', 1, '2026-01-01T00:00:05Z'),
  });
  stream.emit({
    kind: 'workstream-upsert',
    seq: 4,
    workstream: liveWorkstream({
      phase: 'evaluated',
      active: false,
      finishedAt: '2026-01-01T00:00:30Z',
      lastSequence: 2,
      outcome: 'accepted',
      outcomeSummary: 'Settled after the last measurement',
    }),
  });
  stream.emit({kind: 'status', seq: 5, status: 'completed'});

  const state = store.getState();
  expect(state.campaign?.latestTimestamp).toBe('2026-01-01T00:00:30.000Z');
  expect(state.campaign?.status).toBe('completed');
  const finishedAt = state.scenario?.workstreams[0]?.finishedAt;
  expect(finishedAt).toBe('2026-01-01T00:00:30Z');
  expect(Date.parse(finishedAt ?? '')).toBeLessThanOrEqual(
    Date.parse(state.campaign?.latestTimestamp ?? ''),
  );
  store.close();
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
