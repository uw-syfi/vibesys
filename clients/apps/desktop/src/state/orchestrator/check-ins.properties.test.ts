import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  type CheckIn,
  CheckInState,
  Delivery,
  type RunChange,
  RunChangeSchema,
  RunsSnapshotSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {
  askMessageOf,
  checkInOf,
  type Press,
  pressArb,
  pressesToCopies,
  reportMessageOf,
  windowFactsOf,
} from '../../testing/orchestrator/check-ins.js';
import {readCheckIn, rewind, withCheckIn} from './check-ins.js';
import {applyChange, applySnapshot} from './fold.js';
import {emptyState, type OrchestratorState} from './state.js';

const RUN = 'run-1';
const START = Date.parse('2026-11-01T18:00:00Z');
const VIEWERS = ['v1', 'v2', 'v3'] as const;

const presses = fc.array(pressArb, {maxLength: 24});

/** The changes of the run stream that carry these check-in copies, in order, from sequence 1. */
const changesOf = (copies: readonly CheckIn[], from = 0): RunChange[] =>
  copies.map((checkIn, at) =>
    create(RunChangeSchema, {
      sequence: BigInt(from + at + 1),
      runId: RUN,
      at: timestampFromMs(START + at),
      body: {case: 'checkIn', value: checkIn},
    }),
  );

const fold = (changes: readonly RunChange[], from: OrchestratorState = emptyState) =>
  changes.reduce(applyChange, from);

/** What the host serves in a snapshot after these copies: the newest copy of each check-in. */
function snapshotAfter(copies: readonly CheckIn[]) {
  const newest = new Map<string, CheckIn>();
  for (const copy of copies) newest.set(copy.id, copy);
  return create(RunsSnapshotSchema, {
    sequence: BigInt(copies.length),
    checkIns: [...newest.values()],
  });
}

describe('the check-ins a viewer reads back', () => {
  it('are the viewer own windows, newest first, following one another from the start of the run', () => {
    fc.assert(
      fc.property(presses, list => {
        const {copies, finals} = pressesToCopies(RUN, START, list);
        const state = fold(changesOf(copies));
        for (const viewer of VIEWERS) {
          const read = rewind(state, RUN, viewer);
          const mine = finals.filter(checkIn => checkIn.viewer?.id === viewer);
          // Exactly this viewer's check-ins, as the host last said them, and no one else's.
          expect(read.map(each => each.checkIn)).toEqual([...mine].reverse());
          // The ones that did not fail tile the run: no gap and no overlap, oldest first.
          const tiled = read.filter(each => each.checkIn.state !== CheckInState.FAILED).reverse();
          let end = START;
          for (const each of tiled) {
            expect(each.sinceMs).toBe(end);
            expect(each.atMs).toBeGreaterThanOrEqual(each.sinceMs);
            end = each.atMs;
          }
        }
      }),
      {numRuns: 120},
    );
  });

  it('are the same whether they arrive in changes, or after a cut in a snapshot and then changes', () => {
    fc.assert(
      fc.property(presses, fc.nat(), (list, cutAt) => {
        const {copies} = pressesToCopies(RUN, START, list);
        const cut = cutAt % (copies.length + 1);
        const joined = applySnapshot(emptyState, snapshotAfter(copies.slice(0, cut)));
        const resumed = fold(changesOf(copies.slice(cut), cut), joined);
        expect(resumed.checkIns).toEqual(fold(changesOf(copies)).checkIns);
      }),
      {numRuns: 120},
    );
  });

  it('are not changed by a stale copy, whatever order the copies of one check-in arrive in', () => {
    fc.assert(
      fc.property(
        fc.array(pressArb, {minLength: 1, maxLength: 6}),
        fc.array(fc.nat(), {minLength: 1, maxLength: 24}),
        (list, picks) => {
          const {copies} = pressesToCopies(RUN, START, list);
          const arrived = picks.map(pick => copies[pick % copies.length] as CheckIn);
          const state = arrived.reduce(withCheckIn, emptyState);
          // For each check-in the host's copies only go forward, so the one that stays is the
          // furthest along of those that arrived; the same copy twice changes nothing.
          for (const id of new Set(arrived.map(copy => copy.id))) {
            const mine = copies.filter(copy => copy.id === id);
            const furthest = Math.max(
              ...arrived.filter(copy => copy.id === id).map(copy => mine.indexOf(copy)),
            );
            expect(state.checkIns[id]).toBe(mine[furthest]);
          }
          expect(arrived.reduce(withCheckIn, state).checkIns).toEqual(state.checkIns);
        },
      ),
      {numRuns: 150},
    );
  });

  it('drop what a later snapshot does not carry, as the host serves it', () => {
    const copies = pressesToCopies(RUN, START, [
      {viewer: 'v1', afterMs: 10, outcome: 'ready', asked: false},
      {viewer: 'v1', afterMs: 10, outcome: 'ready', asked: false},
    ]).copies;
    const held = fold(changesOf(copies));
    const newest = (copies.filter(copy => copy.id === 'ci-1') as CheckIn[]).slice(-1);
    const later = applySnapshot(
      held,
      create(RunsSnapshotSchema, {sequence: held.sequence + 1n, checkIns: newest}),
    );
    expect(rewind(later, RUN, 'v1').map(each => each.checkIn.id)).toEqual(['ci-1']);
  });
});

describe('a gap in the run stream', () => {
  it('holds back the check-ins after it, which arrive with the snapshot that closes it', () => {
    fc.assert(
      fc.property(presses, fc.nat(), (list, skip) => {
        const {copies} = pressesToCopies(RUN, START, list);
        if (copies.length < 3) return;
        const changes = changesOf(copies);
        const at = skip % (copies.length - 2);
        const held = fold(changes.slice(0, at));
        const gapped = applyChange(held, changes[at + 1] as RunChange);
        expect(gapped.gap).not.toBeNull();
        expect(gapped.checkIns).toEqual(held.checkIns);
        const closed = applySnapshot(gapped, snapshotAfter(copies));
        expect(closed.checkIns).toEqual(fold(changes).checkIns);
      }),
      {numRuns: 80},
    );
  });
});

describe('what a check-in says of its facts', () => {
  const base = {id: 'ci', runId: RUN, viewer: 'v1', sinceMs: START, atMs: START + 1000};

  it('is pending, ready with the frozen facts, or failed with the reason the host gave', () => {
    const facts = windowFactsOf({agentsStarted: 3});
    expect(
      readCheckIn(emptyState, checkInOf({...base, state: CheckInState.PENDING})).facts,
    ).toEqual({kind: 'pending'});
    expect(
      readCheckIn(emptyState, checkInOf({...base, state: CheckInState.READY, facts})).facts,
    ).toEqual({kind: 'ready', facts});
    expect(
      readCheckIn(
        emptyState,
        checkInOf({...base, state: CheckInState.FAILED, failure: 'the job log is away'}),
      ).facts,
    ).toEqual({kind: 'failed', reason: 'the job log is away'});
  });

  it('is told in the window the host served, in milliseconds', () => {
    const read = readCheckIn(emptyState, checkInOf({...base, state: CheckInState.PENDING}));
    expect([read.sinceMs, read.atMs]).toEqual([START, START + 1000]);
  });
});

describe('what a check-in says of the lead summary', () => {
  const withMessages = (...messages: ReturnType<typeof askMessageOf>[]): OrchestratorState => ({
    ...emptyState,
    messages: Object.fromEntries(messages.map(message => [message.id, message])),
  });
  const ready = (fields: {
    ask?: string;
    report?: string;
    facts?: ReturnType<typeof windowFactsOf>;
  }) =>
    checkInOf({
      ...fields,
      id: 'ci',
      runId: RUN,
      viewer: 'v1',
      sinceMs: START,
      atMs: START + 1,
      state: CheckInState.READY,
    });

  const notFinal = [Delivery.QUEUED, Delivery.STEERED, Delivery.NEW_TURN];
  const final = [Delivery.CONSUMED, Delivery.REJECTED];

  it('is writing while the ask is on its way, whatever way that is, and none is linked', () => {
    for (const delivery of notFinal) {
      const state = withMessages(askMessageOf('ask', RUN, START, delivery));
      expect(readCheckIn(state, ready({ask: 'ask'})).summary).toEqual({kind: 'writing'});
    }
  });

  it('is writing when the ask has not reached this client yet', () => {
    expect(readCheckIn(emptyState, ready({ask: 'ask'})).summary).toEqual({kind: 'writing'});
  });

  it('is the linked report once there is one, even if the ask is not final', () => {
    const report = reportMessageOf('rep', RUN, START, 'Best moved to 2.31x', 'Three ideas kept');
    for (const delivery of [...notFinal, ...final]) {
      const state = withMessages(askMessageOf('ask', RUN, START, delivery), report);
      const {summary} = readCheckIn(state, ready({ask: 'ask', report: 'rep'}));
      expect(summary.kind).toBe('report');
      if (summary.kind === 'report') expect(summary.report.headline).toBe('Best moved to 2.31x');
    }
  });

  it('falls back to the reports posted in the window once the ask is final and nothing is linked', () => {
    const posted = reportMessageOf('rep', RUN, START, 'Ideas rejected');
    for (const delivery of final) {
      const state = withMessages(askMessageOf('ask', RUN, START, delivery), posted);
      const facts = windowFactsOf({reportMessageIds: ['rep', 'not-held']});
      const {summary} = readCheckIn(state, ready({ask: 'ask', facts}));
      expect(summary).toEqual({kind: 'reports', reports: [posted.body.value]});
    }
  });

  it('is the window reports for a run that could not be asked, and none when there were none', () => {
    const posted = reportMessageOf('rep', RUN, START, 'Final');
    const state = withMessages(posted);
    const facts = windowFactsOf({reportMessageIds: ['rep']});
    expect(readCheckIn(state, ready({facts})).summary).toEqual({
      kind: 'reports',
      reports: [posted.body.value],
    });
    expect(readCheckIn(state, ready({})).summary).toEqual({kind: 'reports', reports: []});
  });

  it('does not depend on the facts: a failed check-in still shows what the lead wrote', () => {
    const state = withMessages(
      askMessageOf('ask', RUN, START, Delivery.CONSUMED),
      reportMessageOf('rep', RUN, START, 'Written anyway'),
    );
    const failed = checkInOf({
      id: 'ci',
      runId: RUN,
      viewer: 'v1',
      sinceMs: START,
      atMs: START + 1,
      state: CheckInState.FAILED,
      ask: 'ask',
      report: 'rep',
    });
    expect(readCheckIn(state, failed).summary.kind).toBe('report');
  });
});

describe('the press generator', () => {
  it('keeps the host rules the reads rely on', () => {
    fc.assert(
      fc.property(presses, (list: readonly Press[]) => {
        const {finals} = pressesToCopies(RUN, START, list);
        expect(finals).toHaveLength(list.length);
        for (const checkIn of finals) {
          expect(checkIn.state === CheckInState.READY).toBe(checkIn.facts !== undefined);
          expect(checkIn.state === CheckInState.FAILED).toBe(checkIn.failure !== '');
        }
      }),
      {numRuns: 60},
    );
  });
});
