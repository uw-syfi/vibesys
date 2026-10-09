import {describe, expect, it} from 'bun:test';
import {agentKindText, describePhase, phaseText, planningStageForPhase} from './index.js';

const AGENT_LABELS: ReadonlyArray<[string, string | null, string]> = [
  ['round-1-pre', 'orchestrator', 'preparing'],
  ['round-1-plan', 'orchestrator', 'planning'],
  ['round-2-profiler', 'profiler', 'profiling'],
  ['round-1-retry-1-implementer', 'implementer', 'implementing'],
  ['round-1-retry-1-judge', 'judge', 'judging'],
  ['round-3-retry-1-single-agent', 'implementer', 'working'],
  ['round-1-retry-1', 'judge', 'judging'],
  ['round-1', null, 'working'],
];

describe('phase semantics', () => {
  it.each(AGENT_LABELS)('reads %s as an activity', (label, kind, activity) => {
    expect(describePhase(label, kind)?.activity).toBe(activity);
  });

  it('normalizes every loop label family without returning backend syntax', () => {
    expect(phaseText(describePhase('gen-2-cand-1-mutator', 'mutator'))).toBe(
      'mutating candidate 1',
    );
    expect(phaseText(describePhase('impl issue #7 att2', 'implementer'))).toBe(
      'implementing issue #7 · attempt 2',
    );
    expect(phaseText(describePhase('judge issue #7 att1', 'judge'))).toBe('judging issue #7');
    expect(phaseText(describePhase('perf_eval iter 3', 'perf_eval'))).toBe('measuring');
    expect(phaseText(describePhase('experiment-chat', 'chat'))).toBe('answering');
    expect(phaseText(describePhase('some-future-loop-label-7', 'implementer'))).toBe(
      'implementing',
    );
  });

  it('keeps retry attempt semantics and parser precedence', () => {
    expect(phaseText(describePhase('round-1-retry-1-implementer', 'implementer'))).toBe(
      'implementing',
    );
    expect(phaseText(describePhase('round-1-retry-3-implementer', 'implementer'))).toBe(
      'implementing · attempt 3',
    );
    expect(phaseText(describePhase('gen-2-cand-1-mutator', 'judge'))).toBe('mutating candidate 1');
    expect(phaseText(describePhase('round-4-plan', 'chat'))).toBe('answering');
  });

  it('owns planning-stage decisions, including plan retries', () => {
    expect(planningStageForPhase(phase('orchestrator', 'round-3-pre'))).toBe('pre');
    expect(planningStageForPhase(phase('profiler', 'round-3-profiler'))).toBe('profile');
    expect(planningStageForPhase(phase('orchestrator', 'round-3-plan'))).toBe('plan');
    expect(planningStageForPhase(phase('orchestrator', 'round-3-retry-2-plan'))).toBe('plan');
    expect(planningStageForPhase(phase('orchestrator', 'round-3-retry-2-judge'))).toBeNull();
  });

  it('exposes display words without leaking unknown kind identifiers', () => {
    for (const kind of [
      'orchestrator',
      'implementer',
      'judge',
      'profiler',
      'perf_eval',
      'mutator',
      'chat',
    ]) {
      expect(agentKindText(kind)).toEqual(expect.any(String));
    }
    expect(agentKindText('verifier')).toBeNull();
    expect(describePhase(null, null)).toBeNull();
    expect(phaseText(null)).toBeNull();
  });
});

function phase(kind: string, roundLabel: string): {kind: string; roundLabel: string} {
  return {kind, roundLabel};
}
