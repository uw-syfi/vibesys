import {describe, expect, it} from 'bun:test';
import type {ActiveAgentExecution, ExecutionStatus} from '@vibesys/core-state';
import {activityLine, activitySummary, runtimeSuffix, statusSuffix} from './activity-bar.js';

const STARTED_AT = '2026-08-25T12:00:00.000Z';
function execution(
  activity: ActiveAgentExecution['activity'],
  executionId = 'execution-1',
  runtime: {provider?: string | null; model?: string | null} = {},
): ActiveAgentExecution {
  return {
    executionId,
    agentKind: 'implementer',
    roundLabel: 'round-1-implementer',
    roundNumber: 1,
    stage: 'implementation',
    attempt: 1,
    assignment: 'Implement the queue',
    startedAt: STARTED_AT,
    activity,
    ...runtime,
  };
}

describe('activity summary', () => {
  it('uses Working for every activity update', () => {
    expect(activitySummary(execution({mode: 'thinking', summary: ''}))).toBe('Working');
    expect(activitySummary(execution({mode: 'thinking', summary: 'Planning'}))).toBe('Working');
    expect(activitySummary(execution({mode: 'responding', summary: 'Writing a response'}))).toBe(
      'Working',
    );
    expect(
      activitySummary(execution({mode: 'tool', summary: 'Running queue tests', tool: 'Bash'})),
    ).toBe('Working');
    expect(activitySummary(execution({mode: 'waiting', summary: 'Waiting for output'}))).toBe(
      'Working',
    );
  });

  it('uses structured backend progress when available', () => {
    expect(activitySummary(execution({mode: 'thinking', summary: ''}), status())).toBe('Round 1/3');
  });
});

describe('structured execution status', () => {
  it('renders label, backend elapsed time, tokens, and textual context pressure', () => {
    expect(activityLine(execution({mode: 'thinking', summary: ''}), status(), Date.now())).toBe(
      'Implementer 2 · Round 1/3 · 1m 12s · 180k/200k context high 90%',
    );
    expect(statusSuffix(status({inputTokens: 198_000}))).toBe(' · 198k/200k context critical 99%');
  });

  it('falls back to the legacy line when status is absent', () => {
    expect(
      activityLine(
        execution({mode: 'thinking', summary: ''}),
        undefined,
        Date.parse(STARTED_AT) + 5_000,
      ),
    ).toBe('Implementer · Working · 5s');
  });
});

describe('runtime suffix', () => {
  it('renders the harness and model when both are known', () => {
    const exec = execution({mode: 'thinking', summary: ''}, 'execution-1', {
      provider: 'codex',
      model: 'gpt-5.1-codex-max',
    });
    expect(runtimeSuffix(exec)).toBe(' · Codex (GPT 5.1 Codex Max)');
  });

  it('is empty when neither the provider nor the model is known', () => {
    expect(runtimeSuffix(execution({mode: 'thinking', summary: ''}))).toBe('');
  });
});

function status(overrides: Partial<ExecutionStatus> = {}): ExecutionStatus {
  return {
    executionId: 'execution-1',
    sequence: 3,
    observedAt: '2026-08-25T12:01:12.000Z',
    progress: 'Round 1/3',
    agentLabel: 'Implementer 2',
    elapsedSeconds: 72.9,
    inputTokens: 180_000,
    contextWindow: 200_000,
    ...overrides,
  };
}
