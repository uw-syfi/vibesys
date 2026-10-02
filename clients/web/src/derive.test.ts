import {strict as assert} from 'node:assert';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import type {RunEvent} from '@vibesys/backend-client';
import {type CoreState, initialCoreState, reduceEventBatch} from '@vibesys/core-state';
import {
  attachNote,
  endedWord,
  needsOlder,
  pathShortener,
  prose,
  runControl,
  steers,
  steersNeedOlder,
  toolDuration,
} from './derive.js';
import {CAPTURED_TYPES} from './session.js';

const read = (path: string): string => readFileSync(new URL(path, import.meta.url), 'utf8');
const jsonl = (path: string): RunEvent[] =>
  read(path)
    .split('\n')
    .filter(Boolean)
    .map(line => JSON.parse(line) as RunEvent);
// A real agent run: typed tool payloads, one round, interrupted at sequence 627.
const QUEUE = jsonl('../../tui/dev/fixtures/queue-rs-payloads.jsonl');
// A real stub run: 8 rounds, one pause, one steer (pending 212, consumed 215), completed.
const STUB = jsonl('./fixtures/stub-run.jsonl');

const upTo = (events: RunEvent[], sequence: number) =>
  events.filter(event => (event.sequence ?? 0) <= sequence);
const fold = (events: RunEvent[]): CoreState => reduceEventBatch(initialCoreState(), events);
const captured = (events: RunEvent[]) => events.filter(event => CAPTURED_TYPES.has(event.type));
/** The store after a tail bootstrap at `floor`: only events above it are folded and captured. */
const tail = (events: RunEvent[], floor: number) => {
  const above = events.filter(event => (event.sequence ?? 0) > floor);
  return {core: {...fold(above), historyAfterSequence: floor}, held: captured(above)};
};

test('run control comes from core.status and the connection only', () => {
  const at = (status: CoreState['status']): CoreState => ({...initialCoreState(), status});
  const control = (status: CoreState['status'], connection: 'connected' | 'disconnected') => {
    const value = runControl(at(status), [], connection);
    return value.kind === 'action' ? [value.action, value.label, value.disabled] : [value.word];
  };
  assert.deepEqual(control('running', 'connected'), ['pause', 'Pause', false]);
  assert.deepEqual(control('pausing', 'connected'), ['pause', 'Pausing', true]);
  assert.deepEqual(control('paused', 'connected'), ['resume', 'Resume', false]);
  assert.deepEqual(control('starting', 'connected'), ['pause', 'Pause', true]);
  assert.deepEqual(control('connecting', 'connected'), ['pause', 'Pause', true]);
  assert.deepEqual(control('running', 'disconnected'), ['pause', 'Pause', true]);
  assert.deepEqual(control('paused', 'disconnected'), ['resume', 'Resume', true]);
  assert.deepEqual(control('completed', 'connected'), ['Completed']);
  assert.deepEqual(control('failed', 'disconnected'), ['Failed']);
  assert.equal(endedWord(fold(QUEUE), captured(QUEUE)), 'Interrupted');
  assert.equal(endedWord(fold(STUB), captured(STUB)), 'Completed');
  const interrupted = runControl(fold(QUEUE), captured(QUEUE), 'connected');
  assert.deepEqual(interrupted, {
    kind: 'ended',
    word: 'Interrupted',
    summary: fold(QUEUE)
      .diagnostics.filter(item => item.scope === 'run')
      .at(-1)?.summary,
    tip: 'RuntimeError: launcher_terminated (SIGTERM)',
  });
});

test('backfill is offered only where the tail floor may hide a round', () => {
  const core = fold(upTo(STUB, 205));
  assert.equal(needsOlder(core, 1), false, 'full history');
  const tailed = {...core, historyAfterSequence: 120};
  assert.equal(needsOlder(tailed, 1), true);
  assert.equal(needsOlder(tailed, 2), true, "round 1's replayed round_finished is below the floor");
  assert.equal(needsOlder(tailed, 3), false);
  assert.equal(needsOlder(tailed, 0), false, 'R0 has no events to load');
});

test('queued steers: backfill for any started round reaches the latest call start', () => {
  // The stub at 212: paused in round 3, a steer queued after the plan call started at 190.
  const paused = upTo(STUB, 212);
  for (const floor of [212, 195, 189]) {
    assert.equal(needsOlder(tail(paused, floor).core, 3), true, `floor ${floor}`);
  }
  const {core, held} = tail(paused, 150);
  assert.equal(needsOlder(core, 3), false, "round 2's judge call is above the floor");
  assert.deepEqual(
    steers(held).pending.map(steer => steer.text),
    ['Try caching VALUE instead of recomputing it.'],
  );
});

test('consumed steers: backfill reaches the start of the call before the consuming call', () => {
  // The stub's steer: queued during round 3's plan call (190), consumed at 215.
  assert.equal(steersNeedOlder(tail(STUB, 200).core, tail(STUB, 200).held, 3), true);
  assert.equal(steersNeedOlder(tail(STUB, 189).core, tail(STUB, 189).held, 3), false);

  const event = (
    sequence: number,
    type: RunEvent['type'],
    label: string | null,
    kind: string | null,
    extra: Partial<RunEvent> = {},
  ) =>
    ({
      sequence,
      type,
      round_label: label,
      agent_kind: kind,
      timestamp: '2026-09-21T12:00:00Z',
      ...extra,
    }) as RunEvent;
  const said = (sequence: number, label: string, kind: string) =>
    event(sequence, 'agent_output_chunk', label, kind, {
      data: {kind: 'agent_output_chunk', channel: 'analysis', content: `said ${sequence}`},
    });
  const queuedAt = event(11, 'control', null, null, {status: 'pending', text: '/steer: Check it.'});
  const consumedAt = (sequence: number) =>
    event(sequence, 'control', 'round-2-pre', 'orchestrator', {status: 'consumed', text: '/steer'});
  // Queued while round 1's judge ran, consumed by round 2's first call.
  const crossing = [
    event(10, 'phase_started', 'round-1-retry-1-judge', 'judge'),
    queuedAt,
    said(12, 'round-1-retry-1-judge', 'judge'),
    consumedAt(20),
    event(21, 'phase_started', 'round-2-pre', 'orchestrator'),
    said(22, 'round-2-pre', 'orchestrator'),
  ];
  const hidden = tail(crossing, 11);
  assert.equal(steersNeedOlder(hidden.core, hidden.held, 0), false, 'R0 consumed nothing');
  assert.equal(needsOlder(hidden.core, 2), false, "round 1's judge output is above the floor");
  assert.equal(steersNeedOlder(hidden.core, hidden.held, 2), true, 'its start is not');
  assert.deepEqual(steers(hidden.held).consumed, [], 'so the steer text is missing');
  const shown = tail(crossing, 9);
  assert.equal(steersNeedOlder(shown.core, shown.held, 2), false);
  assert.deepEqual(
    steers(shown.held).consumed.map(steer => [steer.round, steer.text]),
    [[2, 'Check it.']],
  );
  // The consuming call's own start can come before its control event; it does not count.
  const started = [
    ...crossing.slice(0, 3),
    event(20, 'phase_started', 'round-2-pre', 'orchestrator'),
    consumedAt(21),
    said(22, 'round-2-pre', 'orchestrator'),
  ];
  assert.equal(steersNeedOlder(tail(started, 11).core, tail(started, 11).held, 2), true);
  assert.equal(steersNeedOlder(tail(started, 9).core, tail(started, 9).held, 2), false);
});

test('prose keeps paragraphs, inline code, and bold; paths shorten at word starts only', () => {
  assert.deepEqual(prose('One `ldar` per **dequeue**.\n\nNext.'), [
    [
      {kind: 'text', text: 'One '},
      {kind: 'code', text: 'ldar'},
      {kind: 'text', text: ' per '},
      {kind: 'strong', text: 'dequeue'},
      {kind: 'text', text: '.'},
    ],
    [{kind: 'text', text: 'Next.'}],
  ]);
  const short = pathShortener('run-7');
  assert.equal(short("cat '/w/run-7/src/a.rs' /x/run-7/b"), "cat 'src/a.rs' b");
  assert.equal(short('cat .vibesys/runs/run-7/x'), 'cat .vibesys/runs/run-7/x');
});

test('tool durations: one decimal under a minute, then minutes and zero-padded seconds', () => {
  const timed = (duration: number) =>
    toolDuration({
      id: '1',
      kind: 'tool',
      content: '',
      toolResult: {
        kind: 'tool_result',
        tool: 'Bash',
        content: '',
        is_error: false,
        payload: {kind: 'command', stdout: '', stderr: '', exit_code: 0, duration},
      },
    });
  assert.deepEqual([0.04, 0.4, 7.9, 12.2, 41.7, 59.99, 65.2].map(timed), [
    '<0.1s',
    '0.4s',
    '7.9s',
    '12.2s',
    '41.7s',
    '1m 00s',
    '1m 05s',
  ]);
});

test('attach note: waiting until the project attaches, or saying the run ended first', () => {
  const experiments = (ready: boolean) => ({request_id: 'q', ok: true, experiments_ready: ready});
  assert.equal(attachNote(null, false), null, 'not loaded yet');
  assert.equal(attachNote(experiments(false), false), 'Waiting for the project to attach');
  assert.equal(attachNote(experiments(false), true), 'The run ended before the project attached');
  assert.equal(attachNote(experiments(true), true), null);
  assert.equal(attachNote(experiments(true), false), null);
});
