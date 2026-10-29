import {describe, expect, test} from 'bun:test';
import type {RunEvent} from '@vibesys/contracts';
import type {LogStream} from '@vibesys/contracts/host-helper';
import {initialCoreState, reduceEventBatch} from '@vibesys/run-state';
import {Seeded} from '@vibesys/store/testing';
import {attached, RUN} from './live-run-fixtures.js';
import type {RunPageState} from './run-page.js';
import {ideaTitlesBySpan, scopedLogs} from './scoped-logs.js';
import {idea} from './timeline-fixtures.js';
import {logGroups} from './views.js';

/**
 * The Logs list of a live run. Regression: gate streams were named by the candidate's revision hash
 * (`accuracy · vs-c5260df6… · attempt 1`), and the startup steps showed a bare triangle and a count
 * (`▸ ×9`) with no word on how long the step took.
 */

type Owner = LogStream['owner'];

function stream(id: string, owner: Owner, live: boolean): LogStream {
  return {id, owner, command: 'cmd', retained_start: 0, size: 0, outcome: live ? null : 'ended'};
}

function pageWith(streams: readonly LogStream[], events: readonly RunEvent[] = []): RunPageState {
  const base = attached();
  const instanceId = base.record.kind === 'live' ? base.record.run.id : 'run';
  return {
    ...base,
    logs: {kind: 'listed', instanceId, streams},
    run: {...base.run, core: reduceEventBatch(initialCoreState(), events)},
  };
}

const HASH = 'vs-c5260df6a49b38a249c58baa3741bab5a4e3ea2e';

function gate(id: string, gateName: string, span?: string): LogStream {
  return stream(
    id,
    {
      kind: 'gate_stage',
      gate: gateName,
      execution_id: HASH,
      attempt: 1,
      ...(span === undefined ? {} : {span_id: span}),
    },
    false,
  );
}

describe('a gate stream is named by its gate and whose measurement it is', () => {
  const ring = idea('Try a ring buffer', 'passed', 0, 60);
  const ideas = ideaTitlesBySpan([
    {...ring, attempts: [{...ring.attempts[0], span: 'flow:1/#1'}]} as typeof ring,
  ]);
  const names = (streams: LogStream[]): string[] =>
    logGroups(pageWith(streams), ideas).flatMap(group => group.streams.map(each => each.label));

  test('the input when the stream names no span, the idea when it names the idea’s span', () => {
    const labels = names([
      gate('a', 'accuracy'),
      gate('b', 'benchmark'),
      gate('c', 'accuracy', 'flow:1/#1'),
      gate('d', 'benchmark', 'op:unknown'),
    ]);
    expect(labels).toEqual([
      'accuracy · input · attempt 1',
      'benchmark · input · attempt 1',
      'accuracy · Try a ring buffer · attempt 1',
      'benchmark · attempt 1',
    ]);
  });

  test('never shows a revision hash, whatever the streams', () => {
    const random = new Seeded(3);
    const gates = ['accuracy', 'benchmark', 'validation', 'build'];
    for (let round = 0; round < 50; round += 1) {
      const streams = Array.from({length: 1 + random.int(8)}, (_, index) =>
        gate(`s${index}`, random.pick(gates), random.bool() ? 'flow:1/#1' : undefined),
      );
      for (const label of names(streams)) expect(label).not.toContain('vs-');
    }
  });
});

describe('a startup step is one row with how long it took', () => {
  const STAGES = ['building_image', 'starting_container'] as const;
  const owner = (stage: (typeof STAGES)[number]): Owner => ({kind: 'startup_step', stage});

  function progress(sequence: number, stage: string, seconds: number): RunEvent {
    const type = 'run_startup_progress';
    return {
      protocol_version: 1,
      run_id: RUN.runId,
      sequence,
      timestamp: '2026-10-17T01:00:00Z',
      type,
      data: {kind: type, stage, state: 'finished', elapsed_seconds: seconds},
    } as RunEvent;
  }

  const restarts = STAGES.flatMap(stage =>
    Array.from({length: 9}, (_, index) => stream(`${stage}-${index}`, owner(stage), false)),
  );
  const events = [progress(1, 'building_image', 95), progress(2, 'starting_container', 7)];

  test('each stage folds into one row, its duration and its count', () => {
    const logs = scopedLogs(pageWith(restarts, events), {kind: 'run'});
    const folds = logs.rows.flatMap(row => (row.kind === 'fold' ? [row] : []));
    expect(folds.map(row => [row.label, row.took, row.count])).toEqual([
      ['Building the agent image', '1m', 9],
      ['Starting the agent container', '7s', 9],
    ]);
  });

  test('a stage that ran once is a row with its duration, and one never reported has none', () => {
    const once = [
      stream('image', owner('building_image'), false),
      stream('box', owner('starting_container'), false),
    ];
    const rows = scopedLogs(pageWith(once, [progress(1, 'building_image', 12)]), {
      kind: 'run',
    }).rows;
    expect(rows.map(row => (row.kind === 'stream' ? row.stream.took : 'fold'))).toEqual([
      '12s',
      null,
    ]);
  });
});
