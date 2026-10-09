import {describe, it} from 'node:test';
import {expect} from '../test-support/expect.js';
import {
  chatEvent,
  event,
  eventBatch,
  roundFinishedEvent,
  snapshotResponse,
  statusEvent,
  timestamp,
} from './index.js';

describe('@vibesys/backend-client/testing', () => {
  it('derives valid deterministic timestamps beyond one-digit sequences', () => {
    expect(timestamp()).toBe('2026-01-01T00:00:00Z');
    expect(timestamp(61)).toBe('2026-01-01T00:01:01Z');
    expect(() => timestamp(-1)).toThrow('Fixture sequence must be a non-negative safe integer');
  });

  it('keeps the generic event neutral and copies supplied data', () => {
    const data = {
      kind: 'agent_output_chunk' as const,
      channel: 'assistant' as const,
      content: 'hello',
    };
    const diagnostic = {
      code: 'fixture_warning',
      summary: 'shared source diagnostic',
      scope: 'run' as const,
    };
    const built = event(2, 'agent_output_chunk', {run_id: 'run-a', diagnostic, data});
    const second = event(2, 'agent_output_chunk', {run_id: 'run-a', diagnostic, data});

    expect(built).toEqual({
      sequence: 2,
      timestamp: '2026-01-01T00:00:02Z',
      type: 'agent_output_chunk',
      run_id: 'run-a',
      diagnostic,
      data,
    });
    expect(built.data).not.toBe(data);
    expect(built.diagnostic).not.toBe(diagnostic);
    expect(second.diagnostic).not.toBe(diagnostic);
    expect(second.diagnostic).not.toBe(built.diagnostic);

    if (built.diagnostic !== null && built.diagnostic !== undefined) {
      built.diagnostic.summary = 'mutated fixture';
    }
    expect(diagnostic.summary).toBe('shared source diagnostic');
    expect(second.diagnostic?.summary).toBe('shared source diagnostic');
    expect(event(undefined, 'server_ready')).toEqual({
      timestamp: '2026-01-01T00:00:00Z',
      type: 'server_ready',
    });
    expect(event(3, 'agent_output_chunk', 'world')).toMatchObject({
      data: {kind: 'agent_output_chunk', channel: 'assistant', content: 'world'},
    });
    expect(() => event(3, 'run_finished', 'invalid')).toThrow(
      'Event content shorthand requires type agent_output_chunk',
    );
  });

  it('uses an unmeasured completed round as the canonical round default', () => {
    expect(roundFinishedEvent(3)).toEqual({
      sequence: 3,
      timestamp: '2026-01-01T00:00:03Z',
      type: 'round_finished',
      status: 'completed',
      round_label: 'round-1',
      data: {
        kind: 'round_finished',
        attempts: 1,
        judge_verdict: 'pass',
        perf_metric: null,
        perf_unit: null,
        profile_skipped: false,
      },
    });

    expect(
      roundFinishedEvent(4, {perf_metric: 900, perf_unit: 'ops/s'}, {round_label: 'round-4'}),
    ).toMatchObject({
      round_label: 'round-4',
      data: {perf_metric: 900, perf_unit: 'ops/s'},
    });
  });

  it('makes status and chat context explicit without inventing chat identity', () => {
    expect(statusEvent(5, 'exec-a', 'tool_call')).toMatchObject({
      execution_id: 'exec-a',
      invocation_id: 'exec-a',
      agent_kind: 'implementer',
      round_label: 'round-1-implementer',
      data: {kind: 'tool_call', call_id: 'call-5', status: {progress: 'step 5'}},
    });
    expect(chatEvent(6, 'chat', {kind: 'chat', answer: 'done'})).toEqual({
      sequence: 6,
      timestamp: '2026-01-01T00:00:06Z',
      type: 'chat',
      agent_kind: 'chat',
      round_label: 'experiment-chat',
      data: {kind: 'chat', answer: 'done'},
    });
  });

  it('owns every mutable default and container it returns', () => {
    const firstRound = roundFinishedEvent(1);
    const secondRound = roundFinishedEvent(1);
    expect(firstRound.data).not.toBe(secondRound.data);

    const source = [event(1, 'server_ready')];
    const firstBatch = eventBatch(source);
    const secondBatch = eventBatch(source);
    expect(firstBatch.events).not.toBe(source);
    expect(firstBatch.events).not.toBe(secondBatch.events);
    expect(firstBatch.events[0]).not.toBe(source[0]);

    const firstSnapshot = snapshotResponse();
    const secondSnapshot = snapshotResponse();
    expect(firstSnapshot.snapshot).not.toBe(secondSnapshot.snapshot);
  });
});
