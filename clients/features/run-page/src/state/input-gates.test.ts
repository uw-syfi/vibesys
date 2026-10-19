import {describe, expect, test} from 'bun:test';
import type {RunEvent} from '@vibesys/contracts';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  failuresAt,
  foldInputGates,
  type GateFailure,
  gateFailureView,
  gateHistoryView,
  gateReason,
  NO_INPUT_GATES,
} from './input-gates.js';

function gate(
  sequence: number,
  status: 'failed' | 'completed',
  tail: string | null,
  name = 'accuracy',
): RunEvent {
  return {
    protocol_version: 1,
    run_id: 'run',
    sequence,
    timestamp: '2026-10-19T07:29:48Z',
    type: 'gate_finished',
    text: '',
    status,
    data: {kind: 'gate_finished', gate: name, recipe: null, output_tail: tail},
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

  test('is never cut, whatever the gate printed', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const line = 'x'.repeat(1 + random.int(400));
      expect(gateReason(`${line}\n`), `seed ${seed}`).toBe(line);
      expect(gateReason(`RuntimeError: ${line}`), `seed ${seed}`).toBe(`RuntimeError: ${line}`);
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
      attempts: 1,
    });
    const two = [gate(7, 'failed', 'oops\n'), gate(8, 'failed', 'oops\n')].reduce(
      foldInputGates,
      NO_INPUT_GATES,
    );
    expect(gateFailureView(two.failures)?.tail).toBe('Accuracy\noops\n\nAccuracy\noops');
    expect(gateFailureView([])).toBeNull();
  });
});

describe('a gate that failed and was tried again', () => {
  const REASON = `RuntimeError: ${'the engine refused to start '.repeat(8)}`.trim();

  test('stands failed until a later attempt of the same gate passes, then is history', () => {
    const events = [gate(1, 'failed', REASON, 'benchmark'), gate(2, 'failed', REASON, 'benchmark')];
    const failing = events.reduce(foldInputGates, NO_INPUT_GATES);
    expect(gateFailureView(failing.failures)?.label).toBe('Benchmark');
    expect(gateHistoryView(failing.failures)).toBeNull();
    const passed = [...events, gate(3, 'completed', null, 'benchmark')].reduce(
      foldInputGates,
      NO_INPUT_GATES,
    );
    expect(passed.failures.map(failure => failure.supersededBy)).toEqual([3, 3]);
    expect(gateFailureView(passed.failures)).toBeNull();
    expect(gateHistoryView(passed.failures)).toMatchObject({label: 'Benchmark', attempts: 2});
  });

  test('keeps the whole reason, whatever its length', () => {
    expect(REASON.length).toBeGreaterThan(200);
    const gates = [gate(1, 'failed', `${REASON}\n`, 'benchmark')].reduce(
      foldInputGates,
      NO_INPUT_GATES,
    );
    expect(gateFailureView(gates.failures)?.reason).toBe(REASON);
  });

  test('is not superseded by a pass of another gate, nor by one before it', () => {
    const gates = [
      gate(1, 'completed', null, 'benchmark'),
      gate(2, 'failed', 'a', 'benchmark'),
      gate(3, 'completed', null, 'accuracy'),
    ].reduce(foldInputGates, NO_INPUT_GATES);
    expect(gates.failures.map(failure => failure.supersededBy)).toEqual([null]);
    expect(gateFailureView(gates.failures)?.label).toBe('Benchmark');
  });

  /** A random run of gate events for two gates, each failed or passed. */
  function randomGates(random: Seeded): RunEvent[] {
    const events: RunEvent[] = [];
    for (let at = 1; at <= 2 + random.int(10); at += 1) {
      const name = random.pick(['accuracy', 'benchmark']);
      events.push(gate(at, random.bool() ? 'failed' : 'completed', 'tail', name));
    }
    return events;
  }

  /** The first event after `failure` that passed the same gate, the one that should supersede it. */
  function firstPassAfter(events: readonly RunEvent[], failure: GateFailure): number | null {
    const later = events.find(
      event =>
        (event.sequence ?? 0) > failure.sequence &&
        event.status !== 'failed' &&
        event.data?.kind === 'gate_finished' &&
        event.data.gate === failure.gate,
    );
    return later?.sequence ?? null;
  }

  test('only its own pass supersedes, wherever the events are cut or replayed', () => {
    for (const seed of SEEDS) {
      const events = randomGates(new Seeded(seed));
      const gates = events.reduce(foldInputGates, NO_INPUT_GATES);
      for (const failure of gates.failures) {
        expect(failure.supersededBy, `seed ${seed}`).toBe(firstPassAfter(events, failure));
      }
      // Replaying every event changes nothing.
      expect([...events, ...events].reduce(foldInputGates, NO_INPUT_GATES).failures).toEqual(
        gates.failures,
      );
    }
  });

  test('a view before the pass holds the failure as it stood then: failed', () => {
    for (const seed of SEEDS) {
      const gates = randomGates(new Seeded(seed)).reduce(foldInputGates, NO_INPUT_GATES);
      for (const failure of gates.failures) {
        if (failure.supersededBy === null) continue;
        const held = failuresAt(gates, failure.supersededBy - 1).find(
          each => each.sequence === failure.sequence,
        );
        expect(held?.supersededBy, `seed ${seed}`).toBeNull();
      }
    }
  });
});

describe('a gate tried again while ideas run', () => {
  test('is superseded by a pass after the first idea began, the failure kept as history', () => {
    const events = [
      gate(1, 'failed', 'ValueError: preflight', 'benchmark'),
      started(2, 'workstream'),
      gate(3, 'completed', null, 'benchmark'),
    ];
    const gates = events.reduce(foldInputGates, NO_INPUT_GATES);
    expect(gateFailureView(gates.failures)).toBeNull();
    expect(gateHistoryView(gates.failures)).toMatchObject({label: 'Benchmark', attempts: 1});
  });

  test('stands failed when no attempt passed, a later failure being the idea’s own', () => {
    const events = [
      gate(1, 'failed', 'a', 'benchmark'),
      started(2, 'workstream'),
      gate(3, 'failed', 'b', 'benchmark'),
      gate(4, 'completed', null, 'accuracy'),
    ];
    const gates = events.reduce(foldInputGates, NO_INPUT_GATES);
    expect(gates.failures.map(failure => failure.supersededBy)).toEqual([null]);
  });

  test('names both attempts when the re-run failed again', () => {
    const gates = [
      gate(1, 'failed', 'oops', 'benchmark'),
      gate(2, 'failed', 'oops', 'benchmark'),
    ].reduce(foldInputGates, NO_INPUT_GATES);
    expect(gateFailureView(gates.failures)).toMatchObject({label: 'Benchmark', attempts: 2});
  });

  test('is read the same wherever the journal is cut: only a pass of its own gate supersedes', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const events = journal(random);
      const gates = events.reduce(foldInputGates, NO_INPUT_GATES);
      for (const failure of gates.failures) {
        const pass = events.find(
          event =>
            (event.sequence ?? 0) > failure.sequence &&
            event.data?.kind === 'gate_finished' &&
            event.status !== 'failed' &&
            event.data.gate === failure.gate,
        );
        expect(failure.supersededBy, `seed ${seed}`).toBe(pass?.sequence ?? null);
      }
    }
  });
});
