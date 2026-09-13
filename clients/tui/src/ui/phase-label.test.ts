import {describe, expect, it} from 'bun:test';
import {agentKindText, describePhase, phaseText} from './phase-label.js';

/** Every label shape the three loops emit, from their `round_label=` sites. */
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

describe('phase description', () => {
  it.each(AGENT_LABELS)('reads %s as an activity', (label, kind, activity) => {
    expect(describePhase(label, kind)?.activity).toBe(activity);
  });

  it('surfaces a retry as an attempt, and stays quiet on the first', () => {
    expect(describePhase('round-1-retry-1-implementer', 'implementer')?.attempt).toBeNull();
    expect(describePhase('round-1-retry-2-implementer', 'implementer')?.attempt).toBe(2);
    expect(phaseText(describePhase('round-1-retry-3-implementer', 'implementer'))).toBe(
      'implementing · attempt 3',
    );
  });

  it('names the candidate in the evolve loop', () => {
    expect(phaseText(describePhase('gen-2-cand-1-mutator', 'mutator'))).toBe(
      'mutating candidate 1',
    );
    expect(phaseText(describePhase('gen-2-cand-3-judge', 'judge'))).toBe('judging candidate 3');
    expect(phaseText(describePhase('gen-1-cand-0-profiler', 'profiler'))).toBe(
      'profiling candidate 0',
    );
  });

  it('names the issue in the plain loop', () => {
    expect(phaseText(describePhase('impl issue #7 att2', 'implementer'))).toBe(
      'implementing issue #7 · attempt 2',
    );
    expect(phaseText(describePhase('judge issue #7 att1', 'judge'))).toBe('judging issue #7');
    expect(phaseText(describePhase('perf_eval iter 3', null))).toBe('measuring');
  });

  it('treats the experiment chat as its own activity, both spellings', () => {
    expect(phaseText(describePhase('experiment chat', 'chat'))).toBe('answering');
    expect(phaseText(describePhase('experiment-chat', 'chat'))).toBe('answering');
  });

  it('applies label-family precedence before ordinary kind fallback', () => {
    expect(phaseText(describePhase('gen-2-cand-1-mutator', 'judge'))).toBe('mutating candidate 1');
    expect(phaseText(describePhase('impl issue #7 att2', 'judge'))).toBe(
      'implementing issue #7 · attempt 2',
    );
    expect(phaseText(describePhase('round-4-plan', 'implementer'))).toBe('planning');
  });

  it('lets the chat kind override a stale loop label', () => {
    expect(phaseText(describePhase('round-4-plan', 'chat'))).toBe('answering');
  });

  it('never returns a raw backend identifier', () => {
    // The acceptance criterion for #517: no round_label reaches the header in
    // any loop mode, including labels this parser does not recognize.
    const labels = [
      ...AGENT_LABELS.map(([label]) => label),
      'gen-2-cand-1-mutator',
      'impl issue #7 att2',
      'judge issue #12 att4',
      'perf_eval iter 9',
      'experiment chat',
      'round-4-retry-2-implementer',
      'some-future-loop-label-7',
    ];
    for (const label of labels) {
      const text = phaseText(describePhase(label, 'implementer'));
      expect(text).not.toBeNull();
      expect(text).not.toContain(label);
      // The internal syntaxes, not the information. `issue #7` is what the
      // operator is working on and belongs on screen; `att2` and `round-1` are
      // identifiers and do not.
      expect(text).not.toMatch(/round-\d|gen-\d|cand-\d|retry-\d|att\d/);
    }
  });

  it('falls back to the agent kind when the label is unknown or absent', () => {
    expect(phaseText(describePhase('some-future-loop-label-7', 'implementer'))).toBe(
      'implementing',
    );
    expect(phaseText(describePhase(null, 'judge'))).toBe('judging');
    expect(phaseText(describePhase('', 'orchestrator'))).toBe('planning');
  });

  it('has nothing to say before a run reports anything', () => {
    expect(describePhase(null, null)).toBeNull();
    expect(phaseText(null)).toBeNull();
  });

  it('degrades to the kind verbatim for an agent kind it does not know', () => {
    // Better a real kind name than a parse failure; still not a round label.
    expect(phaseText(describePhase(null, 'verifier'))).toBe('verifier');
  });

  it('has a word for the plain loop measurement phase', () => {
    // `perf_eval` is a kind, not only a label shape: `expectedRoles` in
    // `run-map.ts` seeds it as a selectable phase for the plain loop, so it
    // reaches the header with no label to parse.
    expect(phaseText(describePhase(null, 'perf_eval'))).toBe('measuring');
    expect(phaseText(describePhase('perf_eval iter 3', 'perf_eval'))).toBe('measuring');
  });
});

describe('agent kind on its own', () => {
  it('has a word for every kind the run map seeds as a selectable phase', () => {
    // The union of `expectedRoles` over the three outer loops, plus the chat.
    // A kind missing here is a backend identifier on an operator's screen.
    const seeded = ['orchestrator', 'implementer', 'judge', 'profiler', 'perf_eval', 'chat'];
    for (const kind of seeded) {
      const word = agentKindText(kind);
      expect({kind, word}).toEqual({kind, word: expect.any(String)});
      expect({kind, leaks: word?.includes(kind) ?? true}).toEqual({kind, leaks: false});
    }
    expect(agentKindText('perf_eval')).toBe('measuring');
    expect(agentKindText('mutator')).toBe('mutating');
  });

  it('says nothing rather than an identifier for a kind it does not know', () => {
    // Deliberately unlike `describePhase`, which degrades to the kind: a
    // caller that can drop the text drops it instead of printing `verifier`.
    expect(agentKindText('verifier')).toBeNull();
    expect(agentKindText(null)).toBeNull();
    expect(agentKindText('')).toBeNull();
  });
});
