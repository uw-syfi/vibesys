import {describe, expect, test} from 'bun:test';
import type {RunEvent} from '@vibesys/contracts';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  failuresAt,
  foldInputGates,
  gateFailureView,
  gateReason,
  NO_INPUT_GATES,
} from './input-gates.js';

function gate(sequence: number, status: 'failed' | 'completed', tail: string | null): RunEvent {
  return {
    protocol_version: 1,
    run_id: 'run',
    sequence,
    timestamp: '2026-10-19T07:29:48Z',
    type: 'gate_finished',
    text: '',
    status,
    data: {kind: 'gate_finished', gate: 'accuracy', recipe: null, output_tail: tail},
  } as RunEvent;
}

function started(sequence: number, kind: string): RunEvent {
  return {
    protocol_version: 1,
    run_id: 'run',
    sequence,
    timestamp: '2026-10-19T07:30:00Z',
    type: 'span_started',
    text: '',
    data: {
      kind: 'span_started',
      span: {
        id: `op:${sequence}`,
        parent: null,
        type: 'operation',
        name: 'x',
        status: 'running',
        started_at: sequence,
        label: {kind, title: 'x'},
      },
    },
  } as RunEvent;
}

function journal(random: Seeded): RunEvent[] {
  const events: RunEvent[] = [];
  for (let at = 1; at <= 1 + random.int(20); at += 1) {
    events.push(
      random.int(4) === 0
        ? started(at, 'workstream')
        : gate(at, random.bool() ? 'failed' : 'completed', 'tail'),
    );
  }
  return events;
}

describe('gateReason', () => {
  test('is the last error the gate printed, with its message', () => {
    expect(
      gateReason("first\nValueError: bad\nthen (ModuleNotFoundError: No module named 'x')\n"),
    ).toBe("ModuleNotFoundError: No module named 'x'");
  });

  test('is the last line it printed when it named no error, and nothing without output', () => {
    expect(gateReason('building\nlinking\n\n')).toBe('linking');
    expect(gateReason(null)).toBeNull();
    expect(gateReason('  \n')).toBeNull();
  });

  test('is never longer than a few words can be, whatever the gate printed', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const line = 'x'.repeat(random.int(400));
      const tail = random.bool() ? `${line}\n` : `RuntimeError: ${line}`;
      expect(gateReason(tail)?.length ?? 0, `seed ${seed}`).toBeLessThanOrEqual(90);
    }
  });
});

describe('the input gates', () => {
  test('hold the failures before the first idea began, and no later one', () => {
    const events = [
      gate(1, 'failed', 'a'),
      gate(2, 'completed', null),
      started(3, 'agent_turn'),
      gate(4, 'failed', 'b'),
      started(5, 'workstream'),
      gate(6, 'failed', 'c'),
    ];
    const gates = events.reduce(foldInputGates, NO_INPUT_GATES);
    expect(gates.failures.map(failure => failure.sequence)).toEqual([1, 4]);
    expect(gates.settled).toBe(true);
  });

  test('are the same wherever the events are cut, and a view holds only what it has reached', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const events = journal(random);
      const gates = events.reduce(foldInputGates, NO_INPUT_GATES);
      const first = events.findIndex(event => event.data?.kind === 'span_started');
      const before = (first === -1 ? events : events.slice(0, first)).filter(
        event => event.status === 'failed',
      );
      expect(
        gates.failures.map(failure => failure.sequence),
        `seed ${seed}`,
      ).toEqual(before.map(event => event.sequence ?? 0));
      const at = random.int(events.length + 1);
      expect(failuresAt(gates, at).every(failure => failure.sequence <= at)).toBe(true);
      expect(failuresAt(gates, null)).toBe(gates.failures);
    }
  });

  test('read as one row: the gates, why, and what they printed', () => {
    const one = [gate(7, 'failed', 'oops\n')].reduce(foldInputGates, NO_INPUT_GATES);
    expect(gateFailureView(one.failures)).toEqual({
      label: 'Accuracy',
      reason: 'oops',
      tail: 'oops',
    });
    const two = [gate(7, 'failed', 'oops\n'), gate(8, 'failed', 'oops\n')].reduce(
      foldInputGates,
      NO_INPUT_GATES,
    );
    expect(gateFailureView(two.failures)?.tail).toBe('Accuracy\noops\n\nAccuracy\noops');
    expect(gateFailureView([])).toBeNull();
  });
});
