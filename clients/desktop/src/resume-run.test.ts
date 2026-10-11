import {describe, expect, test} from 'bun:test';
import {Seeded} from '@vibesys/core-state/testing';
import {
  exactResumeRunId,
  ResumeRequestError,
  RUN_ID_UNAVAILABLE,
  resumeArguments,
  resumeDecision,
} from './resume-run.js';

function generatedRunId(random: Seeded): string {
  const first = 'abcdefghijklmnopqrstuvwxyz0123456789';
  const rest = `${first}._-`;
  let value = random.pick([...first]);
  const length = random.int(128);
  for (let index = 0; index < length; index += 1) value += random.pick([...rest]);
  return value;
}

describe('exact resume identity', () => {
  test('the IPC boundary rejects missing and non-canonical ids with a precise error', () => {
    for (const value of [
      undefined,
      null,
      '',
      ' ',
      ' run-1',
      'run-1 ',
      '-latest',
      '.run-1',
      '_run-1',
      'Run-1',
      'run id',
      '../run',
      'a'.repeat(129),
    ]) {
      expect(() => exactResumeRunId(value)).toThrow(
        'a run id is 1-128 lowercase letters, digits, ".", "_", or "-", starting with a letter or digit',
      );
      expect(() => exactResumeRunId(value)).toThrow(ResumeRequestError);
    }
  });

  test('the IPC boundary accepts canonical ids at both length bounds', () => {
    const longest = `a${'-'.repeat(127)}`;
    expect(String(exactResumeRunId('a'))).toBe('a');
    expect(String(exactResumeRunId(longest))).toBe(longest);
  });

  test('every available run-specific decision emits --resume with one exact id', () => {
    const random = new Seeded(29);
    for (let round = 0; round < 500; round += 1) {
      const candidate = generatedRunId(random);
      const decision = resumeDecision(random.next() < 0.2 ? null : candidate);
      if (decision.kind === 'unavailable') {
        expect(decision.reason).toBe(RUN_ID_UNAVAILABLE);
        continue;
      }
      const args = resumeArguments(decision.runId);
      expect(args.map(String)).toEqual(['--resume', candidate]);
    }
  });
});
