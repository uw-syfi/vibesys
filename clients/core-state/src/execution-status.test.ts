import {describe, expect, it} from 'bun:test';
import type {RunEvent} from '@vibesys/backend-client';
import {
  applyExecutionStatus,
  applyExecutionStatusUsage,
  mergeExecutionStatusesPrefix,
  mergeExecutionStatusUsagePrefix,
} from './execution-status.js';

describe('execution status usage prefix merge', () => {
  it('preserves identity for usage that has no unresolved status provenance', () => {
    const usage = {inputTokens: 9_000, contextWindow: 200_000, model: 'gpt-5'};

    const merged = mergeExecutionStatusUsagePrefix(usage, {}, {}, [], {
      inputTokens: 4_000,
      contextWindow: 100_000,
      model: 'older-model',
    });

    expect(merged).toBe(usage);
  });

  it('combines an older partial status with the suffix update that owns usage', () => {
    const active = {'exec-a': {startedAt: timestamp(1)}};
    const olderEvent = statusEvent(2, 'exec-a', {
      input_tokens: 4_000,
      context_window: 200_000,
    });
    const newerEvent = statusEvent(3, 'exec-a', {input_tokens: 9_000});
    const olderStatuses = applyExecutionStatus({}, olderEvent);
    const newerStatuses = applyExecutionStatus({}, newerEvent);
    const usage = applyExecutionStatusUsage(null, {}, newerStatuses, active, newerEvent);
    const statuses = mergeExecutionStatusesPrefix(olderStatuses, newerStatuses, active);

    const merged = mergeExecutionStatusUsagePrefix(usage, statuses, olderStatuses, [
      startedEvent(1, 'exec-a'),
      olderEvent,
    ]);

    expect(merged).toEqual({inputTokens: 9_000, contextWindow: 200_000, model: null});
  });

  it('does not borrow status fields from an earlier execution generation', () => {
    const active = {'exec-a': {startedAt: timestamp(4)}};
    const olderEvent = statusEvent(2, 'exec-a', {
      input_tokens: 4_000,
      context_window: 200_000,
    });
    const newerEvent = statusEvent(5, 'exec-a', {input_tokens: 9_000});
    const olderStatuses = applyExecutionStatus({}, olderEvent);
    const newerStatuses = applyExecutionStatus({}, newerEvent);
    const usage = applyExecutionStatusUsage(null, {}, newerStatuses, active, newerEvent);

    const merged = mergeExecutionStatusUsagePrefix(usage, newerStatuses, olderStatuses, [
      startedEvent(1, 'exec-a'),
      olderEvent,
      startedEvent(4, 'exec-a'),
    ]);

    expect(merged).toEqual({inputTokens: 9_000, contextWindow: null, model: null});
  });

  it('backfills a model once while retaining suffix token ownership', () => {
    const sourceEvent = statusEvent(3, 'exec-a', {input_tokens: 9_000});
    const statuses = applyExecutionStatus({}, sourceEvent);
    const usage = applyExecutionStatusUsage(
      null,
      {},
      statuses,
      {'exec-a': {startedAt: timestamp(1)}},
      sourceEvent,
    );

    const merged = mergeExecutionStatusUsagePrefix(usage, statuses, {}, [], {
      inputTokens: 4_000,
      contextWindow: 200_000,
      model: 'gpt-5',
    });

    expect(merged).toEqual({inputTokens: 9_000, contextWindow: null, model: 'gpt-5'});
    expect(
      mergeExecutionStatusUsagePrefix(merged, statuses, {}, [], {
        inputTokens: 1_000,
        contextWindow: 100_000,
        model: 'older-model',
      }),
    ).toEqual(merged);
  });
});

function timestamp(sequence: number): string {
  return `2026-01-01T00:00:0${sequence}Z`;
}

function startedEvent(sequence: number, executionId: string): RunEvent {
  return {
    sequence,
    timestamp: timestamp(sequence),
    type: 'agent_execution_started',
    execution_id: executionId,
    agent_kind: 'implementer',
    round_label: 'round-1',
  };
}

function statusEvent(
  sequence: number,
  executionId: string,
  status: {input_tokens: number; context_window?: number},
): RunEvent {
  return {
    sequence,
    timestamp: timestamp(sequence),
    type: 'tool_call',
    execution_id: executionId,
    invocation_id: executionId,
    agent_kind: 'implementer',
    round_label: 'round-1',
    data: {kind: 'tool_call', tool: 'Bash', call_id: `call-${sequence}`, args: {}, status},
  };
}
