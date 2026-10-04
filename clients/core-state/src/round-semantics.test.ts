import {describe, expect, it} from 'bun:test';
import type {HypothesisEntry, RunEvent} from '@vibesys/backend-client';
import {timestamp} from '@vibesys/backend-client/testing';
import {
  activeRunFocus,
  initialCoreState,
  joinRoundsWithExperiments,
  type RoundState,
  reduceEventBatch,
  reduceEventPrefix,
  roundKeyFor,
  roundOutcome,
  roundsWithPlan,
} from './index.js';

describe('round identity', () => {
  it('uses a tagged numeric key when the known grammar extracts a number', () => {
    expect(roundKeyFor({round_label: 'round-12-retry-2-judge'})).toEqual({
      kind: 'number',
      number: 12,
    });
    expect(roundKeyFor({round_label: 'iteration 7'})).toEqual({kind: 'number', number: 7});
  });

  it('uses exact label equality as the fallback identity', () => {
    expect(roundKeyFor({round_label: 'gen-1-cand-0-mutator'})).toEqual({
      kind: 'label',
      label: 'gen-1-cand-0-mutator',
    });
    expect(roundKeyFor({round_label: null})).toBeNull();
  });

  it('keeps evolve labels as grouped unnumbered rounds with scoped phases', () => {
    const state = reduceEventBatch(initialCoreState(), [
      executionEvent(1, 'agent_execution_started', 'mutator-1', 'gen-1-cand-0-mutator', 'mutator'),
      outputEvent(2, 'gen-1-cand-0-mutator', 'mutating'),
      executionEvent(3, 'agent_execution_finished', 'mutator-1', 'gen-1-cand-0-mutator', 'mutator'),
      roundFinished(4, 'gen-1-cand-0-mutator'),
      executionEvent(5, 'agent_execution_started', 'judge-1', 'gen-1-cand-0-judge', 'judge'),
    ]);

    expect(state.rounds.map(round => round.key)).toEqual([
      {kind: 'label', label: 'gen-1-cand-0-mutator'},
      {kind: 'label', label: 'gen-1-cand-0-judge'},
    ]);
    expect(state.rounds.map(round => round.number)).toEqual([null, null]);
    expect(state.rounds[0]?.status).toBe('completed');
    expect(state.phases.filter(phase => phase.executionId !== undefined)).toMatchObject([
      {
        executionId: 'mutator-1',
        roundKey: {kind: 'label', label: 'gen-1-cand-0-mutator'},
        roundNumber: null,
        status: 'completed',
      },
      {
        executionId: 'judge-1',
        roundKey: {kind: 'label', label: 'gen-1-cand-0-judge'},
        roundNumber: null,
        status: 'active',
      },
    ]);
  });

  it('preserves fallback identity and first appearance across a prefix merge', () => {
    const events = [
      outputEvent(1, 'future-loop-beta', 'older beta'),
      outputEvent(2, 'future-loop-alpha', 'older alpha'),
      outputEvent(3, 'future-loop-beta', 'newer beta'),
      roundFinished(4, 'future-loop-beta'),
      roundFinished(5, 'future-loop-alpha'),
    ];
    const full = reduceEventBatch(initialCoreState(), events);
    const tail = reduceEventBatch(initialCoreState(), events.slice(2), undefined, undefined, 2);
    const merged = reduceEventPrefix(tail, events.slice(0, 2), 0);

    expect(merged).toEqual(full);
    expect(merged.rounds.map(round => round.key)).toEqual([
      {kind: 'label', label: 'future-loop-beta'},
      {kind: 'label', label: 'future-loop-alpha'},
    ]);
  });
});

describe('round projections', () => {
  const entries: HypothesisEntry[] = [
    {
      hypothesis_id: 'H-1',
      first_round: 1,
      last_round: 1,
      rounds: [{round: 1, passed: false, reviewed: true, judge_verdict: 'fail'}],
    },
    {hypothesis_id: 'H-2', first_round: 2, last_round: 2, rounds: []},
  ];

  it('produces planned numeric rows while retaining unnumbered rows', () => {
    const unknown = labelRound('future-loop-alpha', 'active');
    const projected = roundsWithPlan([numberedRound(1, 'completed'), unknown], 3);

    expect(projected.map(round => round.key)).toEqual([
      {kind: 'number', number: 1},
      {kind: 'number', number: 2},
      {kind: 'number', number: 3},
      {kind: 'label', label: 'future-loop-alpha'},
    ]);
    expect(projected.map(round => round.status)).toEqual([
      'completed',
      'planned',
      'planned',
      'active',
    ]);
  });

  it('joins numbered rounds to experiment ownership without inventing an unnumbered join', () => {
    const joined = joinRoundsWithExperiments(
      [
        numberedRound(1, 'completed'),
        numberedRound(2, 'active'),
        labelRound('future-loop', 'active'),
      ],
      entries,
    );

    expect(joined[0]).toMatchObject({hypothesis: {hypothesis_id: 'H-1'}, record: {round: 1}});
    expect(joined[1]).toMatchObject({hypothesis: {hypothesis_id: 'H-2'}, record: null});
    expect(joined[2]).toMatchObject({hypothesis: null, record: null});
  });

  it('applies outcome precedence through the public projection', () => {
    expect(roundOutcome(numberedRound(1, 'active'), null)).toBe('live');
    expect(roundOutcome(numberedRound(1, 'planned'), null)).toBe('planned');
    expect(roundOutcome(numberedRound(1, 'failed'), null)).toBe('fail');
    expect(roundOutcome(numberedRound(1, 'completed'), entries[0]?.rounds?.[0] ?? null)).toBe(
      'fail',
    );
    expect(roundOutcome({...numberedRound(1, 'completed'), profileSkipped: true}, null)).toBe(
      'skipped',
    );
    expect(roundOutcome(numberedRound(1, 'completed'), null)).toBe('done');
  });
});

describe('active run focus', () => {
  it('derives concurrent focus from executions and their run-map phases, not cursor fields', () => {
    const folded = reduceEventBatch(initialCoreState(), [
      executionEvent(1, 'agent_execution_started', 'later-id', 'round-2-judge', 'judge'),
      executionEvent(2, 'agent_execution_started', 'earlier-id', 'gen-1-cand-0-mutator', 'mutator'),
    ]);
    const state = {...folded, agentKind: 'stale-cursor', roundLabel: 'round-99-stale'};

    expect(activeRunFocus(state)).toMatchObject([
      {
        executionId: 'later-id',
        agentKind: 'judge',
        roundKey: {kind: 'number', number: 2},
        description: {activity: 'judging'},
      },
      {
        executionId: 'earlier-id',
        agentKind: 'mutator',
        roundKey: {kind: 'label', label: 'gen-1-cand-0-mutator'},
        description: {activity: 'mutating', subject: 'candidate 0'},
      },
    ]);
  });

  it('does not attach a reused execution id to a completed phase from an older scope', () => {
    const state = reduceEventBatch(initialCoreState(), [
      executionEvent(1, 'agent_execution_started', 'reused', 'round-1-implementer', 'implementer'),
      executionEvent(2, 'agent_execution_finished', 'reused', 'round-1-implementer', 'implementer'),
      executionEvent(3, 'agent_execution_started', 'reused', 'round-2-judge', 'judge'),
    ]);

    expect(state.phases).toMatchObject([
      {
        executionId: 'reused',
        status: 'completed',
        kind: 'implementer',
        roundKey: {kind: 'number', number: 1},
      },
      {
        executionId: 'reused',
        status: 'active',
        kind: 'judge',
        roundKey: {kind: 'number', number: 2},
      },
    ]);
    expect(activeRunFocus(state)).toMatchObject([
      {
        executionId: 'reused',
        agentKind: 'judge',
        roundKey: {kind: 'number', number: 2},
        roundNumber: 2,
        description: {activity: 'judging'},
      },
    ]);
  });
});

function numberedRound(number: number, status: RoundState['status']): RoundState {
  return {key: {kind: 'number', number}, number, status};
}

function labelRound(label: string, status: RoundState['status']): RoundState {
  return {key: {kind: 'label', label}, number: null, status};
}

function outputEvent(sequence: number, roundLabel: string, content: string): RunEvent {
  return {
    sequence,
    timestamp: timestamp(sequence),
    type: 'agent_output_chunk',
    agent_kind: 'mutator',
    round_label: roundLabel,
    invocation_id: `invocation-${sequence}`,
    data: {kind: 'agent_output_chunk', channel: 'assistant', content},
  };
}

function roundFinished(sequence: number, roundLabel: string): RunEvent {
  return {
    sequence,
    timestamp: timestamp(sequence),
    type: 'round_finished',
    status: 'completed',
    round_label: roundLabel,
  };
}

function executionEvent(
  sequence: number,
  type: 'agent_execution_started' | 'agent_execution_finished',
  executionId: string,
  roundLabel: string,
  agentKind: string,
): RunEvent {
  return {
    sequence,
    timestamp: timestamp(sequence),
    type,
    status: type === 'agent_execution_started' ? 'active' : 'completed',
    execution_id: executionId,
    invocation_id: executionId,
    agent_kind: agentKind,
    round_label: roundLabel,
    data:
      type === 'agent_execution_started'
        ? {
            kind: 'agent_execution_started',
            stage: agentKind,
            attempt: 1,
            system_prompt: '',
            user_prompt: `Run ${agentKind}`,
            activity: {
              kind: 'agent_execution_activity_changed',
              mode: 'thinking',
              summary: `Starting ${agentKind}`,
              tool: null,
            },
          }
        : {kind: 'agent_execution_finished', error: null},
  };
}
