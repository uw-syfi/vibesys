import {describe, expect, test} from 'bun:test';
import {event, eventBatch, roundFinishedEvent, timestamp} from '@vibesys/contracts/testing';
import {FakeLifecycle, sessionWith} from '@vibesys/run-session/testing';
import {initialCoreState, reduceEvent} from '@vibesys/run-state';
import {
  type CampaignProjectionState,
  initialCampaignProjection,
  projectCampaignFrames,
  webSessionCampaignStream,
} from './live-campaign-stream.js';

function usageEvent(
  sequence: number,
  roundLabel: string,
  agentKind: string,
  tokens: {input_tokens?: number; output_tokens?: number} = {},
) {
  return event(sequence, 'turn_usage_recorded', {
    agent_kind: agentKind,
    round_label: roundLabel,
    data: {kind: 'turn_usage_recorded', input_tokens: 10, output_tokens: 2, ...tokens},
  });
}

function finishedEvent(
  sequence: number,
  roundLabel: string,
  overrides: {
    repeated_error?: boolean;
    last_error?: {kind: string; text?: string};
  } = {},
) {
  return event(sequence, 'workstream_finished', {
    round_label: roundLabel,
    data: {
      kind: 'workstream_finished',
      repeated_error: false,
      ...overrides,
    } as never,
  });
}

function benchmarkEvent(sequence: number, roundLabel: string) {
  return event(sequence, 'benchmark_result', {
    round_label: roundLabel,
    data: {kind: 'benchmark_result', metric: 'tok_per_sec', value: 42, unit: 'tok/s'},
  });
}

describe('projectCampaignFrames', () => {
  test('reports the campaign header once, from a fresh run', () => {
    const {frames, state} = projectCampaignFrames(initialCampaignProjection(), initialCoreState());

    expect(frames).toHaveLength(1);
    expect(frames[0]).toMatchObject({kind: 'campaign-init', seq: 1});
    expect(state.headerSent).toBe(true);
    expect(state.completed).toBe(false);
  });

  test('reports nothing when core state has not changed since the last call', () => {
    const core = initialCoreState();
    const first = projectCampaignFrames(initialCampaignProjection(), core);
    const second = projectCampaignFrames(first.state, core);

    expect(second.frames).toEqual([]);
  });

  test('reports an agent, a workstream, and a tokens frame for one scoped turn', () => {
    const core = reduceEvent(initialCoreState(), usageEvent(1, 'round-1', 'implementer'));
    const {frames} = projectCampaignFrames(initialCampaignProjection(), core);

    expect(frames.map(frame => frame.kind)).toEqual([
      'campaign-init',
      'agent-upsert',
      'workstream-upsert',
      'tokens',
    ]);
    expect(frames.map(frame => frame.seq)).toEqual([1, 2, 3, 4]);
    expect(frames[1]).toMatchObject({
      kind: 'agent-upsert',
      agent: {id: 'implementer', name: 'Implementer', role: 'implementer'},
    });
    const workstreamFrame = frames[2];
    expect(workstreamFrame?.kind === 'workstream-upsert' && workstreamFrame.workstream).toEqual({
      id: 'round-1',
      title: 'Round 1',
      hypothesis: '',
      startedAt: timestamp(1),
      firstSequence: 1,
      phase: 'implementing',
      active: true,
      finishedAt: null,
      lastSequence: null,
      outcome: null,
      outcomeSummary: '',
    });
    expect(frames[3]).toMatchObject({kind: 'tokens', workstreamId: 'round-1', tokens: 12});
  });

  test('on a later call, reports only what changed: a new agent and the updated token total', () => {
    const core1 = reduceEvent(initialCoreState(), usageEvent(1, 'round-1', 'implementer'));
    const first = projectCampaignFrames(initialCampaignProjection(), core1);

    const core2 = reduceEvent(
      core1,
      usageEvent(2, 'round-1', 'reviewer', {input_tokens: 5, output_tokens: 1}),
    );
    const {frames} = projectCampaignFrames(first.state, core2);

    expect(frames.map(frame => frame.kind)).toEqual(['agent-upsert', 'tokens']);
    expect(frames[0]).toMatchObject({agent: {id: 'reviewer'}});
    // 10 + 2 (first turn) + 5 + 1 (second turn) = 18 cumulative tokens.
    expect(frames[1]).toMatchObject({kind: 'tokens', workstreamId: 'round-1', tokens: 18});
  });

  test('reports an inconclusive measurement while the workstream is still active', () => {
    const core = reduceEvent(initialCoreState(), benchmarkEvent(1, 'round-2'));
    const {frames} = projectCampaignFrames(initialCampaignProjection(), core);

    const measurementFrame = frames.find(frame => frame.kind === 'measurement');
    expect(measurementFrame?.kind === 'measurement' && measurementFrame.measurement).toMatchObject({
      workstreamId: 'round-2',
      label: 'tok_per_sec',
      values: [{metricId: 'tok_per_sec', value: 42, unit: 'tok/s'}],
      disposition: 'inconclusive',
    });
  });

  test('marks the workstream accepted and later measurements accepted once it finishes cleanly', () => {
    let core = reduceEvent(initialCoreState(), usageEvent(1, 'round-3', 'implementer'));
    core = reduceEvent(core, finishedEvent(2, 'round-3'));
    core = reduceEvent(core, benchmarkEvent(3, 'round-3'));

    const {frames} = projectCampaignFrames(initialCampaignProjection(), core);

    const workstreamFrame = frames.find(frame => frame.kind === 'workstream-upsert');
    expect(
      workstreamFrame?.kind === 'workstream-upsert' && workstreamFrame.workstream,
    ).toMatchObject({
      phase: 'evaluated',
      active: false,
      outcome: 'accepted',
      outcomeSummary: 'Workstream completed',
    });
    const measurementFrame = frames.find(frame => frame.kind === 'measurement');
    expect(
      measurementFrame?.kind === 'measurement' && measurementFrame.measurement.disposition,
    ).toBe('accepted');
  });

  test('marks the workstream rejected, with its failure text, once it finishes with an error', () => {
    let core = reduceEvent(initialCoreState(), usageEvent(1, 'round-4', 'implementer'));
    core = reduceEvent(
      core,
      finishedEvent(2, 'round-4', {
        repeated_error: true,
        last_error: {kind: 'out-of-budget', text: 'ran out of turns'},
      }),
    );
    core = reduceEvent(core, benchmarkEvent(3, 'round-4'));

    const {frames} = projectCampaignFrames(initialCampaignProjection(), core);

    const workstreamFrame = frames.find(frame => frame.kind === 'workstream-upsert');
    expect(
      workstreamFrame?.kind === 'workstream-upsert' && workstreamFrame.workstream,
    ).toMatchObject({
      phase: 'failed',
      active: false,
      outcome: 'rejected',
      outcomeSummary: 'ran out of turns',
    });
    const measurementFrame = frames.find(frame => frame.kind === 'measurement');
    expect(
      measurementFrame?.kind === 'measurement' && measurementFrame.measurement.disposition,
    ).toBe('rejected');
  });

  test('skips a round that has not recorded a start time yet', () => {
    const core = reduceEvent(
      initialCoreState(),
      roundFinishedEvent(1, {}, {round_label: 'round-9'}),
    );

    const {frames} = projectCampaignFrames(initialCampaignProjection(), core);

    expect(frames.some(frame => frame.kind === 'workstream-upsert')).toBe(false);
  });

  test('emits a terminal status frame, last, exactly once the run ends', () => {
    let core = reduceEvent(initialCoreState(), usageEvent(1, 'round-1', 'implementer'));
    core = reduceEvent(core, event(2, 'run_failed'));

    const {frames, state} = projectCampaignFrames(initialCampaignProjection(), core);

    expect(frames.at(-1)).toMatchObject({kind: 'status', status: 'completed'});
    expect(state.completed).toBe(true);
  });

  test('reports nothing after the run has ended, even if core state keeps changing', () => {
    let core = reduceEvent(initialCoreState(), usageEvent(1, 'round-1', 'implementer'));
    core = reduceEvent(core, event(2, 'run_failed'));
    const ended = projectCampaignFrames(initialCampaignProjection(), core);
    expect(ended.state.completed).toBe(true);

    const mutated = reduceEvent(core, usageEvent(3, 'round-1', 'implementer'));
    const after = projectCampaignFrames(ended.state, mutated);

    expect(after.frames).toEqual([]);
    expect(after.state).toEqual(ended.state satisfies CampaignProjectionState);
  });
});

describe('webSessionCampaignStream', () => {
  /** Let the transport's and the session's promise callbacks run: microtasks only, no timer. */
  async function settle(): Promise<void> {
    for (let turn = 0; turn < 50; turn += 1) await Promise.resolve();
  }

  test('reports frames derived from the session it is given, as its folded state changes', async () => {
    const lifecycle = new FakeLifecycle();
    const {session, transport} = sessionWith(lifecycle);
    await session.start();

    const stream = webSessionCampaignStream(session);
    const received: string[] = [];
    const errors: Error[] = [];
    const unsubscribe = stream.subscribe(
      frame => received.push(frame.kind),
      error => errors.push(error),
    );

    expect(received).toEqual(['campaign-init']);

    const subscription = transport.subscriptions[0];
    if (subscription === undefined) throw new Error('Initial subscription did not open');
    subscription.onMessage(
      eventBatch([usageEvent(2, 'round-1', 'implementer')], {
        store_id: 'store-1',
        through_sequence: 2,
        history_after_sequence: 0,
      }),
    );
    await settle();

    expect(received).toEqual(['campaign-init', 'agent-upsert', 'workstream-upsert', 'tokens']);
    expect(errors).toEqual([]);

    unsubscribe();
    subscription.onMessage(
      eventBatch([usageEvent(3, 'round-1', 'implementer')], {
        store_id: 'store-1',
        through_sequence: 3,
        history_after_sequence: 0,
      }),
    );
    await settle();

    // Unsubscribing stops further reports even though the session keeps folding.
    expect(received).toEqual(['campaign-init', 'agent-upsert', 'workstream-upsert', 'tokens']);

    await session.close();
  });
});
