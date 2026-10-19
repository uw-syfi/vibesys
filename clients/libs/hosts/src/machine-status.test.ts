import {describe, expect, it} from 'bun:test';
import type {ConnectionStatus} from './connection-status.js';
import {
  connectionProblem,
  MACHINE_PROBLEMS,
  type MachineProblem,
  mostUrgentProblem,
  problemWord,
} from './machine-status.js';

describe('problemWord', () => {
  it('gives each problem its own word, an old helper reading as an update, never as unreachable', () => {
    const words = MACHINE_PROBLEMS.map(problemWord);
    expect(new Set(words).size).toBe(MACHINE_PROBLEMS.length);
    expect(problemWord('needs-update')).toBe('Needs an update');
    expect(problemWord('unavailable')).toBe('Unreachable');
  });
});

describe('connectionProblem', () => {
  const detail = 'why';
  const cases: readonly (readonly [ConnectionStatus, MachineProblem | null])[] = [
    [{kind: 'connecting'}, null],
    [{kind: 'connected'}, null],
    [{kind: 'reconnecting', attempt: 2}, null],
    [{kind: 'offline', detail}, 'unavailable'],
    [{kind: 'auth-needed', detail}, 'sign-in'],
    [{kind: 'incompatible', detail}, 'needs-update'],
    [{kind: 'run-ended'}, null],
    [{kind: 'closed'}, null],
    [{kind: 'capacity', detail}, null],
  ];
  for (const [connection, problem] of cases) {
    it(`reads ${connection.kind} as ${problem ?? 'no problem'}`, () => {
      expect(connectionProblem(connection)).toBe(problem);
    });
  }
});

describe('mostUrgentProblem', () => {
  it('is none for none, and is the same whatever order the problems come in', () => {
    expect(mostUrgentProblem([])).toBeNull();
    for (let mask = 1; mask < 2 ** MACHINE_PROBLEMS.length; mask += 1) {
      const some = MACHINE_PROBLEMS.filter((_, index) => (mask & (1 << index)) !== 0);
      const expected = some[0];
      expect(mostUrgentProblem(some)).toBe(expected ?? null);
      expect(mostUrgentProblem([...some].reverse())).toBe(expected ?? null);
    }
  });
});
