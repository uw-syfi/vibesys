/**
 * The folds of what the host serves of a run beside the run stream: its summary, replaced whole, and
 * its history, read on request in pages that meet the stream's changes in any order.
 */
import {create} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  type GetRunHistoryResponse,
  RunSummarySchema,
  RunsSnapshotSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {FakeDaemon, generateScenario} from '../../testing/orchestrator/index.js';
import {applyChange, applyRunHistory, applySnapshot, applySummary} from './fold.js';
import {emptyState, type OrchestratorState} from './state.js';

const scenarios = fc.record({
  seed: fc.integer({min: 1, max: 3000}),
  subagents: fc.integer({min: 0, max: 9}),
  live: fc.boolean(),
});

/** The history of the run at the daemon's sequence, as pages of `size` messages. */
function pages(daemon: FakeDaemon, runId: string, size: number): GetRunHistoryResponse[] {
  const out: GetRunHistoryResponse[] = [];
  let cursor = 0n;
  while (out.length < 1000) {
    const page = daemon.history(runId, cursor, size);
    out.push(page);
    if (page.nextCursor === 0n) break;
    cursor = page.nextCursor;
  }
  return out;
}

const foldPages = (
  state: OrchestratorState,
  runId: string,
  list: readonly GetRunHistoryResponse[],
): OrchestratorState =>
  list.reduce(
    (held, page, at) =>
      applyRunHistory(held, {runId, page, first: at === 0, last: at === list.length - 1}),
    state,
  );

const historyOf = (state: OrchestratorState) => ({
  agents: state.agents,
  turns: state.turns,
  messages: state.messages,
});

describe('the history of a run read on request', () => {
  it('meets the run stream in any order: a client ends with the whole history', () => {
    fc.assert(
      fc.property(
        scenarios,
        fc.nat(),
        fc.nat(),
        fc.integer({min: 1, max: 40}),
        (options, held, read, size) => {
          const scenario = generateScenario(options);
          const total = scenario.changes.length;
          const a = held % (total + 1);
          const b = read % (total + 1);
          // The client holds the stream's changes up to `a`; the host's pages are current to `b`.
          const before = scenario.changes.slice(0, a).reduce(applyChange, emptyState);
          const daemon = new FakeDaemon(scenario, b);
          const runId = scenario.runId;
          const after = foldPages(before, runId, pages(daemon, runId, size));
          // The stream goes on to the end, as it does whichever of the two came first.
          const caughtUp = scenario.changes.reduce(applyChange, after);
          const whole = scenario.changes.reduce(applyChange, emptyState);
          expect(historyOf(caughtUp)).toEqual(historyOf(whole));
          expect(caughtUp.histories[runId]?.complete).toBe(true);
        },
      ),
      {numRuns: 80},
    );
  });

  it('is read whole whatever size its pages are', () => {
    fc.assert(
      fc.property(
        scenarios,
        fc.integer({min: 1, max: 40}),
        fc.integer({min: 1, max: 40}),
        (options, one, other) => {
          const scenario = generateScenario(options);
          const daemon = new FakeDaemon(scenario);
          const runId = scenario.runId;
          expect(historyOf(foldPages(emptyState, runId, pages(daemon, runId, one)))).toEqual(
            historyOf(foldPages(emptyState, runId, pages(daemon, runId, other))),
          );
        },
      ),
      {numRuns: 40},
    );
  });

  it('is forgotten by a snapshot, and a page of a history that was forgotten is of nothing', () => {
    const scenario = generateScenario({seed: 9, subagents: 3, live: false});
    const daemon = new FakeDaemon(scenario);
    const runId = scenario.runId;
    const list = pages(daemon, runId, 2);
    expect(list.length).toBeGreaterThan(2);
    const first = list[0];
    if (first === undefined) throw new Error('no pages');
    const reading = applyRunHistory(emptyState, {runId, page: first, first: true, last: false});
    expect(reading.histories[runId]?.complete).toBe(false);
    // A snapshot arrives between two pages: the run is listed, and none of its history is held.
    const resynced = applySnapshot(
      reading,
      create(RunsSnapshotSchema, {sequence: daemon.sequence, runs: daemon.snapshot().runs}),
    );
    expect(resynced.histories).toEqual({});
    expect(Object.keys(resynced.messages)).toEqual([]);
    // The pages that follow are of nothing, so the history is never taken for whole.
    const rest = list
      .slice(1)
      .reduce(
        (held, page, at) =>
          applyRunHistory(held, {runId, page, first: false, last: at === list.length - 2}),
        resynced,
      );
    expect(rest).toEqual(resynced);
  });
});

describe('the summary of a run', () => {
  const summary = (runId: string, working: number) =>
    create(RunSummarySchema, {runId, agentsWorking: working, agentsTotal: working});

  it('replaces the one held for its run, and no other', () => {
    fc.assert(
      fc.property(
        fc.array(fc.tuple(fc.constantFrom('a', 'b', 'c'), fc.nat(9)), {maxLength: 12}),
        sent => {
          const state = sent.reduce(
            (held, [runId, working]) => applySummary(held, summary(runId, working)),
            emptyState,
          );
          for (const runId of ['a', 'b', 'c']) {
            const last = sent.filter(([id]) => id === runId).at(-1);
            expect(state.summaries[runId]?.agentsWorking).toBe(last?.[1]);
          }
        },
      ),
    );
  });

  it('is replaced, with what the host left out, by every snapshot: those of runs it no longer lists go', () => {
    const held = applySummary(applySummary(emptyState, summary('a', 1)), summary('b', 2));
    const after = applySnapshot(
      held,
      create(RunsSnapshotSchema, {
        sequence: 5n,
        summaries: [summary('a', 7)],
        truncated: true,
        runsLeftOut: 12,
      }),
    );
    expect(Object.keys(after.summaries)).toEqual(['a']);
    expect(after.summaries['a']?.agentsWorking).toBe(7);
    expect(after.cut).toEqual({truncated: true, runsLeftOut: 12});
    // An older snapshot (a host that restarted its numbers) replaces them just the same.
    const older = applySnapshot(
      {...after, sequence: 50n},
      create(RunsSnapshotSchema, {sequence: 3n, summaries: [summary('b', 1)], runsLeftOut: 0}),
    );
    expect(Object.keys(older.summaries)).toEqual(['b']);
    expect(older.cut).toEqual({truncated: false, runsLeftOut: 0});
  });
});
