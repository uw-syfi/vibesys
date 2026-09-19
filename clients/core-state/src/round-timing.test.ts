import {describe, expect, it} from 'bun:test';
import {
  closeActiveAgentTimings,
  finishAgentTiming,
  mergeAgentTimingPrefix,
  type RoundTimingState,
  startAgentTiming,
} from './round-timing.js';

describe('agent timing prefix merge', () => {
  it('joins a suffix finish to its prefix start', () => {
    const older = startAgentTiming({}, event(1, 'agent_execution_started', 'exec-a'));
    const newer = finishAgentTiming({}, event(2, 'agent_execution_finished', 'exec-a'));

    expect(mergeAgentTimingPrefix(older, newer)).toEqual({
      agentIntervals: [{startedAt: timestamp(1), finishedAt: timestamp(2)}],
      activeAgentStarts: {},
    });
  });

  it('retains an unmatched finish so a later prefix can supply its start', () => {
    const finish = finishAgentTiming({}, event(3, 'agent_execution_finished', 'exec-a'));
    const partial = mergeAgentTimingPrefix({}, finish);
    const start = startAgentTiming({}, event(1, 'agent_execution_started', 'exec-a'));

    expect(mergeAgentTimingPrefix(start, partial)).toEqual({
      agentIntervals: [{startedAt: timestamp(1), finishedAt: timestamp(3)}],
      activeAgentStarts: {},
    });
  });

  it('orders replay by sequence when prefix and suffix timestamps collide', () => {
    const sharedTimestamp = timestamp(1);
    const older = startAgentTiming(
      {},
      {...event(1, 'agent_execution_started', 'exec-a'), timestamp: sharedTimestamp},
    );
    const newer = startAgentTiming(
      finishAgentTiming(
        {},
        {...event(2, 'agent_execution_finished', 'exec-a'), timestamp: sharedTimestamp},
      ),
      {...event(3, 'agent_execution_started', 'exec-a'), timestamp: sharedTimestamp},
    );

    expect(mergeAgentTimingPrefix(older, newer)).toEqual({
      agentIntervals: [{startedAt: sharedTimestamp, finishedAt: sharedTimestamp}],
      activeAgentStarts: {'implementer:exec-a': sharedTimestamp},
    });
  });

  it('deduplicates a compatibility finish after the modern finish', () => {
    const older = startAgentTiming({}, event(1, 'agent_execution_started', 'exec-a'));
    let newer = finishAgentTiming({}, event(2, 'agent_execution_finished', 'exec-a'));
    newer = finishAgentTiming(newer, event(3, 'phase_finished', 'exec-a'));

    expect(mergeAgentTimingPrefix(older, newer).agentIntervals).toEqual([
      {startedAt: timestamp(1), finishedAt: timestamp(2)},
    ]);
  });

  it('applies a suffix closeout to every active prefix timing', () => {
    let older: RoundTimingState = startAgentTiming(
      {},
      event(1, 'agent_execution_started', 'exec-a'),
    );
    older = startAgentTiming(older, event(2, 'agent_execution_started', 'exec-b'));
    const newer = closeActiveAgentTimings({}, timestamp(4), 4);

    expect(mergeAgentTimingPrefix(older, newer)).toEqual({
      agentIntervals: [
        {startedAt: timestamp(1), finishedAt: timestamp(4)},
        {startedAt: timestamp(2), finishedAt: timestamp(4)},
      ],
      activeAgentStarts: {},
    });
  });

  it('keeps persisted intervals without replay provenance as fixed facts', () => {
    const fixed = {agentIntervals: [{startedAt: timestamp(1), finishedAt: timestamp(2)}]};

    expect(mergeAgentTimingPrefix(fixed, {})).toEqual(fixed);
  });
});

function timestamp(sequence: number): string {
  return `2026-01-01T00:00:0${sequence}Z`;
}

function event(
  sequence: number,
  type: 'agent_execution_started' | 'agent_execution_finished' | 'phase_finished',
  executionId: string,
) {
  return {
    sequence,
    timestamp: timestamp(sequence),
    type,
    execution_id: executionId,
    agent_kind: 'implementer',
  };
}
