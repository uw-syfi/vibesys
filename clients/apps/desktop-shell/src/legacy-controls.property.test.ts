import {expect, test} from 'bun:test';
import {validateRunEvent} from '@vibesys/contracts';
import {event} from '@vibesys/contracts/testing';
import {headerView, runPageInitial, runPageStep} from '@vibesys/feature-run-page/state';
import type {HostKey, HostRun} from '@vibesys/hosts';
import {initialCoreState, reduceEvent} from '@vibesys/run-state';
import fc from 'fast-check';

const INPUT = fc.record({
  terminal: fc.constantFrom('completed', 'failed', 'stopped'),
  listed: fc.constantFrom('starting', 'running', 'pausing', 'paused', 'stopping'),
  ended: fc.boolean(),
  resumable: fc.boolean(),
  sequence: fc.integer({min: 2, max: 1000}),
});

type Input = typeof INPUT extends fc.Arbitrary<infer Value> ? Value : never;

function listedRun(input: Input): HostRun {
  return {
    id: 'instance-a',
    runId: 'run-1',
    projectRoot: '/project',
    startedAt: 1,
    status: input.ended ? input.terminal : input.listed,
    phase: input.ended ? input.terminal : input.listed,
    paused: input.listed === 'paused' ? 'user' : null,
    reason: null,
    end: input.ended
      ? {
          outcome: input.terminal === 'completed' ? 'adopted' : input.terminal,
          reason: 'Run ended',
          resumable: input.resumable,
        }
      : null,
  };
}

test('property: legacy controls follow terminal journal facts despite stale listings and late events, and Resume follows host admission', () => {
  fc.assert(
    fc.property(INPUT, input => {
      const seed = {
        identity: {host: 'local' as HostKey, runId: 'run-1'},
        title: {project: '/project', task: 'mpmc'},
        hostLabel: 'host-a',
        unavailable: {},
      };
      const run = listedRun(input);
      let state = runPageStep(runPageInitial(seed), {
        type: 'record',
        record: {kind: input.ended ? 'ended' : 'live', run},
      }).state;
      if (state.link.kind === 'attaching')
        state = runPageStep(state, {type: 'attached', token: state.link.token, error: null}).state;
      const terminal = event(input.sequence, 'run_status_changed', {
        data: {kind: 'run_status_changed', status: input.terminal, previous: 'running'},
      });
      const stale = event(1, 'run_status_changed', {
        data: {kind: 'run_status_changed', status: 'running', previous: 'starting'},
      });
      const core = [terminal, stale].reduce(
        (core, value) => reduceEvent(core, validateRunEvent(JSON.parse(JSON.stringify(value)))),
        initialCoreState(),
      );
      state = runPageStep(state, {type: 'run-core', core}).state;
      expect(headerView(state).pause.shown).toBe(false);
      expect(headerView(state).pauseNow.shown).toBe(false);
      // The host admits Resume after every ending, whatever flag its record keeps for old readers.
      expect(headerView(state).resume.shown).toBe(true);
      expect(runPageStep(state, {type: 'resume-ended'}).requests).toHaveLength(1);
      for (const type of ['pause', 'ask-pause-now', 'confirm-pause-now'] as const)
        expect(runPageStep(state, {type}).requests).toEqual([]);
    }),
    {numRuns: 80, seed: 229},
  );
});
