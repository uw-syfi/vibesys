import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {BuildVersionsSchema} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import type {HostConnectionState} from './connection.js';
import {FAULT_KINDS, type Fault} from './fault.js';
import {
  applyMachineEvent,
  emptyMachineStats,
  type MachineBounds,
  type MachineEnd,
  type MachineEvent,
  type MachineSample,
  type MachineStats,
  machineRetry,
  machineView,
  nextLapse,
  openedAt,
  type Receipt,
  type Receipts,
  received,
} from './machine-stats.js';

const daemon = create(BuildVersionsSchema, {
  binaryVersion: 'dev',
  protocolVersion: 1,
  minProtocolVersion: 1,
});
const ready: HostConnectionState = {_tag: 'Ready', epoch: 1, daemon};
const lostLink: Fault = {kind: 'Unavailable', detail: 'link lost', retryable: true};
const notReady = fc.constantFrom<HostConnectionState>(
  {_tag: 'Idle'},
  {_tag: 'Connecting', attempt: 0},
  {_tag: 'Waiting', attempt: 2, fault: lostLink, retryAtMs: 5000},
  {_tag: 'Incompatible', side: 'daemon-too-old', daemon},
  {_tag: 'Unusable', fault: lostLink},
);

/** Wide enough that no generated stream breaks it, unless a test means it to. */
const MANY = 1000;
const KEYS = ['host', 'other', 'third'] as const;

const sample: fc.Arbitrary<MachineSample> = fc.record({
  cpuPercent: fc.oneof(
    fc.record({_tag: fc.constant('Read' as const), value: fc.double({min: 0, max: 100})}),
    fc.record({_tag: fc.constant('Absent' as const), cause: fc.constant('no-tool' as const)}),
  ),
  memory: fc.constant({_tag: 'Read', value: {usedBytes: 1, totalBytes: 2}} as const),
  gpus: fc.oneof(
    fc.constant({_tag: 'Read', value: []} as const),
    fc.constant({_tag: 'Absent', cause: 'timed-out'} as const),
  ),
});
const fault: fc.Arbitrary<Fault> = fc.record({
  kind: fc.constantFrom(...FAULT_KINDS),
  detail: fc.string(),
  retryable: fc.boolean(),
});
const end: fc.Arbitrary<MachineEnd> = fc.oneof(
  fc.record({
    _tag: fc.constant('Refused' as const),
    refusal: fc.constantFrom('unsupported', 'over-limit', 'source-failed', 'internal' as const),
  }),
  fc.constant({_tag: 'Silent'} as const),
  fc.constant({_tag: 'Closed'} as const),
  fc.record({_tag: fc.constant('Invalid' as const), detail: fc.string()}),
  fc.record({_tag: fc.constant('Failed' as const), fault}),
);
const change: fc.Arbitrary<Extract<MachineEvent, {_tag: 'Entry' | 'Removed'}>> = fc.oneof(
  fc.record({_tag: fc.constant('Entry' as const), key: fc.constantFrom(...KEYS), sample}),
  fc.record({_tag: fc.constant('Removed' as const), key: fc.constantFrom(...KEYS)}),
);
const event: fc.Arbitrary<MachineEvent> = fc.oneof(
  change,
  fc.constantFrom<MachineEvent>({_tag: 'Open'}, {_tag: 'Synced'}, {_tag: 'Aged'}, {_tag: 'Stop'}),
  end.map(why => ({_tag: 'End', why}) as const),
);

const fold = (
  events: readonly MachineEvent[],
  from: MachineStats = emptyMachineStats,
  max = MANY,
) => events.reduce((model, next) => applyMachineEvent(model, next, max), from);

/** What `changes` leave of `keys`, in order, the last one for a key winning. */
const replay = (
  changes: readonly Extract<MachineEvent, {_tag: 'Entry' | 'Removed'}>[],
  keys: ReadonlyMap<string, MachineSample> = new Map(),
) => {
  const out = new Map(keys);
  for (const one of changes) {
    if (one._tag === 'Entry') out.set(one.key, one.sample);
    else out.delete(one.key);
  }
  return out;
};

const OPEN: MachineEvent = {_tag: 'Open'};
const SYNCED: MachineEvent = {_tag: 'Synced'};

describe('the machine stats fold', () => {
  it('changes nothing for a frame that comes with no stream open', () => {
    fc.assert(
      fc.property(fc.array(event), change, (before, late) => {
        const model = fold([...before, {_tag: 'End', why: {_tag: 'Closed'}}]);
        for (const frame of [late, SYNCED, {_tag: 'Aged'} as const]) {
          expect(applyMachineEvent(model, frame, MANY)).toBe(model);
        }
      }),
    );
  });

  it('shows a snapshot only once it is whole, then applies each change in order', () => {
    fc.assert(
      fc.property(
        fc.array(event),
        fc.array(change),
        fc.array(change),
        (before, snapshot, after) => {
          const opened = fold([...before, OPEN]);
          const reading = fold(snapshot, opened);
          // Whatever the snapshot holds so far, what is shown is the last whole one.
          expect(reading.shown).toBe(opened.shown);
          const synced = applyMachineEvent(reading, SYNCED, MANY);
          expect(synced.shown).toEqual(replay(snapshot));
          expect(synced.ended).toBeNull();
          expect(fold(after, synced).shown).toEqual(replay(after, replay(snapshot)));
        },
      ),
    );
  });

  it('keeps what is shown, and why the last stream ended, while a new stream reads its snapshot', () => {
    fc.assert(
      fc.property(fc.array(change), end, fc.array(change), (first, why, second) => {
        const shown = fold([OPEN, ...first, SYNCED]);
        const reopened = fold([{_tag: 'End', why}, OPEN, ...second], shown);
        expect(reopened.shown).toEqual(shown.shown);
        expect(reopened.ended).toEqual(why);
        expect(machineView(ready, reopened)).toMatchObject({
          _tag: shown.shown?.size ? 'Shown' : 'Missing',
        });
      }),
    );
  });

  it('ends a stream that brings more keys than the topic allows, as invalid, keeping what is shown', () => {
    fc.assert(
      fc.property(sample, sample, fc.boolean(), (one, two, afterSync) => {
        const shown = fold([OPEN, {_tag: 'Entry', key: 'host', sample: one}, SYNCED], undefined, 1);
        // Either the stream that synced, or a new one whose snapshot holds the key so far.
        const opened = afterSync
          ? shown
          : fold(
              [
                {_tag: 'End', why: {_tag: 'Silent'}},
                OPEN,
                {_tag: 'Entry', key: 'host', sample: one},
              ],
              shown,
              1,
            );
        const broken = applyMachineEvent(opened, {_tag: 'Entry', key: 'other', sample: two}, 1);
        expect(broken.open).toBe(false);
        expect(broken.ended).toEqual({_tag: 'Invalid', detail: 'more than 1 key'});
        expect(broken.shown).toEqual(shown.shown);
        // The same key again is a new value, not a second key.
        expect(applyMachineEvent(opened, {_tag: 'Entry', key: 'host', sample: two}, 1).open).toBe(
          true,
        );
      }),
    );
  });

  it('forgets everything when nobody follows the topic', () => {
    fc.assert(
      fc.property(fc.array(event), before => {
        expect(applyMachineEvent(fold(before), {_tag: 'Stop'}, MANY)).toBe(emptyMachineStats);
      }),
    );
  });

  it('holds its own invariants after any events', () => {
    fc.assert(
      fc.property(fc.array(event, {maxLength: 40}), fc.integer({min: 1, max: 3}), (events, max) => {
        const model = fold(events, emptyMachineStats, max);
        if (model.reading !== null) expect(model.open).toBe(true);
        if (model.aged) expect(model.reading).toBeNull();
        expect(model.shown?.size ?? 0).toBeLessThanOrEqual(max);
        expect(model.reading?.size ?? 0).toBeLessThanOrEqual(max);
      }),
    );
  });

  it('marks a value stale only after its snapshot, and the next value makes it current', () => {
    fc.assert(
      fc.property(sample, sample, (one, two) => {
        const shown = fold([OPEN, {_tag: 'Entry', key: 'host', sample: one}, SYNCED]);
        expect(applyMachineEvent(fold([OPEN]), {_tag: 'Aged'}, MANY).aged).toBe(false);
        const aged = applyMachineEvent(shown, {_tag: 'Aged'}, MANY);
        expect(machineView(ready, aged)).toEqual({
          _tag: 'Shown',
          sample: one,
          stale: 'not-updating',
        });
        // A removal is a change, not a new value.
        const removed = applyMachineEvent(aged, {_tag: 'Removed', key: 'other'}, MANY);
        expect(removed.aged).toBe(true);
        const renewed = applyMachineEvent(removed, {_tag: 'Entry', key: 'host', sample: two}, MANY);
        expect(machineView(ready, renewed)).toEqual({_tag: 'Shown', sample: two, stale: null});
      }),
    );
  });
});

describe('what the feed does after a stream ends', () => {
  it('waits out a refusal no retry helps, and a broken contract, until the link is new', () => {
    expect(machineRetry({_tag: 'Refused', refusal: 'unsupported'})).toBe('next-link');
    expect(machineRetry({_tag: 'Invalid', detail: 'x'})).toBe('next-link');
    for (const refusal of ['over-limit', 'source-failed', 'internal'] as const) {
      expect(machineRetry({_tag: 'Refused', refusal})).toBe('backoff');
    }
    expect(machineRetry({_tag: 'Silent'})).toBe('backoff');
    expect(machineRetry({_tag: 'Closed'})).toBe('backoff');
  });

  it('hands a failure that says the link is down to the connection, and keeps the rest from it', () => {
    fc.assert(
      fc.property(fault, failure => {
        const retry = machineRetry({_tag: 'Failed', fault: failure});
        if (failure.kind === 'FellBehind') expect(retry).toBe('backoff');
        else if (failure.retryable || failure.kind === 'NoBridge') expect(retry).toBe('lost');
        else expect(retry).toBe('next-link');
      }),
    );
  });
});

describe('what the screen shows of the machine', () => {
  const one: MachineSample = {
    cpuPercent: {_tag: 'Read', value: 12},
    memory: {_tag: 'Read', value: {usedBytes: 1, totalBytes: 4}},
    gpus: {_tag: 'Read', value: []},
  };
  const current = fold([OPEN, {_tag: 'Entry', key: 'host', sample: one}, SYNCED]);

  it('is the reading, current, while the stream is open and synced on a ready link', () => {
    expect(machineView(ready, current)).toEqual({_tag: 'Shown', sample: one, stale: null});
  });

  it('keeps a reading on screen and says why it is not current', () => {
    fc.assert(
      fc.property(notReady, connection => {
        expect(machineView(connection, current)).toEqual({
          _tag: 'Shown',
          sample: one,
          stale: 'disconnected',
        });
      }),
    );
    const reopened = fold([{_tag: 'End', why: {_tag: 'Silent'}}, OPEN], current);
    expect(machineView(ready, reopened)).toMatchObject({stale: 'silent'});
    const between = fold([{_tag: 'End', why: {_tag: 'Closed'}}], current);
    expect(machineView(ready, between)).toMatchObject({stale: 'not-updating'});
    const resynced = fold([OPEN, {_tag: 'Entry', key: 'host', sample: one}, SYNCED], reopened);
    expect(machineView(ready, resynced)).toMatchObject({stale: null});
  });

  it('waits, with nothing read, while the link is dialed and the first snapshot is read', () => {
    for (const connection of [{_tag: 'Idle'}, {_tag: 'Connecting', attempt: 1}] as const) {
      expect(machineView(connection, emptyMachineStats)).toEqual({_tag: 'Waiting'});
    }
    expect(machineView(ready, emptyMachineStats)).toEqual({_tag: 'Waiting'});
    expect(machineView(ready, fold([OPEN]))).toEqual({_tag: 'Waiting'});
  });

  it('says why there is no reading, for every way a stream ends', () => {
    const missing = (why: MachineEnd) => machineView(ready, fold([OPEN, {_tag: 'End', why}]));
    expect(missing({_tag: 'Refused', refusal: 'unsupported'})).toEqual({
      _tag: 'Missing',
      why: 'unsupported',
    });
    expect(missing({_tag: 'Refused', refusal: 'source-failed'})).toMatchObject({
      why: 'read-failed',
    });
    expect(missing({_tag: 'Refused', refusal: 'over-limit'})).toMatchObject({why: 'over-limit'});
    expect(missing({_tag: 'Refused', refusal: 'internal'})).toMatchObject({why: 'failed'});
    expect(missing({_tag: 'Silent'})).toMatchObject({why: 'silent'});
    expect(missing({_tag: 'Invalid', detail: 'x'})).toMatchObject({why: 'bad-readings'});
    const failed = (kind: Fault['kind'], retryable: boolean) =>
      missing({_tag: 'Failed', fault: {kind, detail: '', retryable}});
    // A host built before the topic answers the call as unimplemented.
    expect(failed('Unimplemented', false)).toMatchObject({why: 'unsupported'});
    expect(failed('InvalidMessage', false)).toMatchObject({why: 'bad-readings'});
    expect(failed('Unavailable', true)).toMatchObject({why: 'disconnected'});
    expect(failed('Refused', false)).toMatchObject({why: 'failed'});
    expect(machineView(ready, fold([OPEN, SYNCED]))).toEqual({_tag: 'Missing', why: 'no-readings'});
  });
});

describe('the bounds on a stream', () => {
  const bounds: MachineBounds = {leaseMs: 30_000, firstFrameMs: 15_000, staleAfterMs: 30_000};
  const receipt = fc.constantFrom<Receipt>('heartbeat', 'data', 'value');
  /** Receipts of a stream opened at `start`, after frames that came `gaps` apart. */
  const receiptsOf = (start: number, frames: readonly (readonly [number, Receipt])[]) => {
    let at = start;
    return frames.reduce<Receipts>((so, [gap, kind]) => {
      at += gap;
      return received(so, kind, at);
    }, openedAt(start));
  };
  const frames = fc.array(fc.tuple(fc.nat({max: 40_000}), receipt), {maxLength: 12});
  const models = fc.constantFrom(
    fold([OPEN]),
    fold([OPEN, SYNCED]),
    fold([OPEN, SYNCED, {_tag: 'Aged'}]),
  );

  it('measures only gaps: moving every receipt by the same amount moves the lapse by it', () => {
    fc.assert(
      fc.property(
        models,
        frames,
        fc.integer({min: -1e12, max: 1e12}),
        fc.integer({min: 0, max: 1e12}),
        (model, seen, start, shift) => {
          const here = nextLapse(model, receiptsOf(start, seen), bounds);
          const there = nextLapse(model, receiptsOf(start + shift, seen), bounds);
          expect(there).toEqual({at: here.at + shift, lapse: here.lapse});
        },
      ),
    );
  });

  it('holds the stream to its lease, the snapshot to its wait, and a value to its age', () => {
    fc.assert(
      fc.property(models, frames, (model, seen) => {
        const so = receiptsOf(0, seen);
        const {at, lapse} = nextLapse(model, so, bounds);
        const lease = so.frame + bounds.leaseMs;
        expect(at).toBeLessThanOrEqual(lease);
        if (model.reading !== null) expect(at).toBe(Math.min(lease, so.data + bounds.firstFrameMs));
        else if (model.aged) expect({at, lapse}).toEqual({at: lease, lapse: 'lease'});
        else expect(at).toBe(Math.min(lease, so.value + bounds.staleAfterMs));
        if (lapse === 'value') expect(model.reading === null && !model.aged).toBe(true);
      }),
    );
  });

  it('never brings a lapse closer when a frame arrives', () => {
    fc.assert(
      fc.property(models, frames, fc.nat({max: 40_000}), receipt, (model, seen, gap, kind) => {
        const so = receiptsOf(0, seen);
        const later = Math.max(so.frame, so.data, so.value) + gap;
        expect(nextLapse(model, received(so, kind, later), bounds).at).toBeGreaterThanOrEqual(
          nextLapse(model, so, bounds).at,
        );
      }),
    );
  });

  it('leaves a topic with no age bound to the lease alone', () => {
    const synced = fold([OPEN, SYNCED]);
    expect(nextLapse(synced, openedAt(0), {...bounds, staleAfterMs: 0})).toEqual({
      at: bounds.leaseMs,
      lapse: 'lease',
    });
  });
});
