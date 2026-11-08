import {describe, expect, it} from '@effect/vitest';
import {oneofIs, type RunChange} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {settingsStreams} from '../../testing/orchestrator/settings-changes.js';
import {applyChange} from './fold.js';
import {
  changeOf,
  type LastWrite,
  lastWrite,
  settingsHistory,
  settingsTimeline,
} from './run-settings.js';
import {emptyState, type OrchestratorState} from './state.js';

const fold = (changes: readonly RunChange[]): OrchestratorState =>
  changes.reduce(applyChange, emptyState);

const BODY_KINDS = {
  run: true,
  agent: true,
  turn: true,
  item: true,
  message: true,
  receipt: true,
  checkIn: true,
  runUpdated: true,
} as const;

const RUNS = ['run-a', 'run-b', 'run-c'];

/** The last-write state computed straight from the events: no fold, no state. */
function lastWriteOf(changes: readonly RunChange[], runId: string): LastWrite {
  let written: LastWrite = {};
  for (const event of changes) {
    const {body} = event;
    if (!oneofIs(body, 'runUpdated', BODY_KINDS) || body.value.runId !== runId) continue;
    for (const change of body.value.changes) {
      const view = changeOf(change);
      if (view === null) continue;
      written = {...written, [view.kind]: view.to};
    }
  }
  return written;
}

describe('the run_updated fold', () => {
  it('over any event sequence equals the last-write state of each run', () => {
    fc.assert(
      fc.property(settingsStreams, changes => {
        const state = fold(changes);
        for (const runId of RUNS)
          expect(lastWrite(settingsHistory(state, runId))).toEqual(lastWriteOf(changes, runId));
      }),
    );
  });

  it('lists each run its own changes, oldest first by sequence and newest first on screen', () => {
    fc.assert(
      fc.property(settingsStreams, changes => {
        const state = fold(changes);
        for (const runId of RUNS) {
          const expected = changes.filter(c => c.runId === runId).map(c => c.sequence);
          expect(settingsHistory(state, runId).map(row => row.sequence)).toEqual(expected);
          expect(settingsTimeline(state, runId).map(row => row.sequence)).toEqual(
            [...expected].reverse(),
          );
        }
      }),
    );
  });

  it('is unchanged by repeats and by a stream cut and resumed anywhere', () => {
    fc.assert(
      fc.property(settingsStreams, fc.nat(), (changes, cut) => {
        const at = cut % (changes.length + 1);
        const resumed = changes.slice(at).reduce(applyChange, fold(changes.slice(0, at)));
        expect(resumed.settingChanges).toEqual(fold(changes).settingChanges);
        expect(changes.reduce(applyChange, fold(changes)).settingChanges).toEqual(
          fold(changes).settingChanges,
        );
      }),
    );
  });

  it('holds a skipped event off the timeline until the gap closes', () => {
    fc.assert(
      fc.property(settingsStreams, fc.nat(), (changes, skip) => {
        fc.pre(changes.length >= 2);
        const at = skip % (changes.length - 1);
        const without = changes.filter((_, index) => index !== at);
        const state = fold(without);
        expect(state.sequence).toBe(BigInt(at));
        const held = Object.values(state.settingChanges)
          .flat()
          .sort((a, b) => Number(a.sequence - b.sequence));
        expect(held.map(record => record.sequence)).toEqual(
          changes.slice(0, at).map(change => change.sequence),
        );
      }),
    );
  });
});
