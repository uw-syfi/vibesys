import {describe, expect, it} from 'bun:test';
import type {RunEvent, RunSnapshot} from '@vibesys/backend-client';
import type {
  AgentPhase,
  CoreDiagnostic,
  CoreState,
  RoundState,
  RunLifetimeBoundary,
  TranscriptEntry,
} from './index.js';
import * as coreState from './index.js';

describe('published core-state surface', () => {
  it('exports only projection entry points and read helpers at runtime', () => {
    expect(Object.keys(coreState).sort()).toEqual([
      'DEFAULT_CHAT_THREAD_ID',
      'activeRunFocus',
      'agentKindText',
      'describePhase',
      'executionStatusFor',
      'experimentForRound',
      'hasActiveAgentTiming',
      'hasRunEnded',
      'hypothesisRoundNumbers',
      'initialCoreState',
      'joinRoundsWithExperiments',
      'latestDiagnosticChange',
      'phaseText',
      'phasesForRound',
      'planningStageForPhase',
      'reconcileActiveExecutions',
      'recordsBenchmark',
      'reduceEvent',
      'reduceEventBatch',
      'reduceEventPrefix',
      'reduceEventRebootstrap',
      'reduceResponseEvents',
      'reduceSnapshot',
      'roundAgentElapsedMs',
      'roundKeyFor',
      'roundOutcome',
      'roundsWithPlan',
      'sameRoundKey',
    ]);
  });

  it('rejects writes throughout a development projection without corrupting another snapshot', () => {
    withNodeEnvironment('development', () => {
      let first = coreState.reduceEvent(coreState.initialCoreState(), executionStarted());
      first = coreState.reduceEvent(first, outputChunk(2, 'hello'));
      const second = coreState.reduceSnapshot(first, runningSnapshot(3));

      expect(first.rounds).toBe(second.rounds);
      expect(first.transcript).toBe(second.transcript);
      expectFrozenReachableReferences(second);
      expect(() =>
        (first.rounds as unknown as RoundState[]).push({
          key: {kind: 'number', number: 2},
          number: 2,
          status: 'planned',
        }),
      ).toThrow(TypeError);
      expect(() =>
        (first.transcript as unknown as TranscriptEntry[]).push(
          first.transcript[0] as TranscriptEntry,
        ),
      ).toThrow(TypeError);
      expect(() => {
        (first.rounds[0] as unknown as {status: string}).status = 'failed';
      }).toThrow(TypeError);
      expect(second.rounds).toHaveLength(1);
      expect(second.rounds[0]).toMatchObject({number: 1, status: 'active'});
      expect(second.transcript.map(entry => entry.content)).toEqual(['hello']);

      // A consumer reconstruction has no hidden run-map index. Folding it
      // proves the reducer replaces frozen entries instead of mutating them.
      const appended = coreState.reduceEvent({...second}, outputChunk(4, ' world'));
      const continued = coreState.reduceEvent(appended, roundFinished(5));
      expect(continued.rounds[0]?.status).toBe('completed');
      expect(continued.transcript.map(entry => entry.content)).toContain('hello world');
    });
  });

  it('publishes cyclic protocol values without freezing caller-owned input', () => {
    withNodeEnvironment('development', () => {
      const arguments_: Record<string, unknown> = {};
      arguments_['self'] = arguments_;
      arguments_['nested'] = {value: 'original'};
      arguments_['items'] = [{value: 'original'}];
      const first = coreState.reduceEvent(coreState.initialCoreState(), toolCall(1, arguments_));
      const second = coreState.reduceSnapshot(first, runningSnapshot(2));
      const published = first.transcript[0]?.toolArguments;
      const nested = published?.['nested'];
      const items = published?.['items'];

      expect(published).not.toBe(arguments_);
      expect(published?.['self']).toBe(published);
      expect(Object.isFrozen(published)).toBe(true);
      expect(Object.isFrozen(nested)).toBe(true);
      expect(Array.isArray(items)).toBe(true);
      expect(Object.isFrozen(items)).toBe(true);
      expect(() => {
        (nested as {value: string}).value = 'changed';
      }).toThrow(TypeError);
      expect(() => {
        (items as unknown as unknown[]).push({value: 'changed'});
      }).toThrow(TypeError);
      expect(second.transcript[0]?.toolArguments?.['nested']).toEqual({value: 'original'});
      expect(second.transcript[0]?.toolArguments?.['items'] as unknown).toEqual([
        {value: 'original'},
      ]);
      expect(Object.isFrozen(arguments_)).toBe(false);
      expect(Object.isFrozen(arguments_['nested'])).toBe(false);
      expect(Object.isFrozen(arguments_['items'])).toBe(false);
    });
  });

  it('omits publication guards in production', () => {
    withNodeEnvironment('production', expectPublicationGuardsDisabled);
  });

  it('treats an unset Node environment as production-safe', () => {
    withNodeEnvironment(undefined, expectPublicationGuardsDisabled);
  });
});

function executionStarted(): RunEvent {
  return {
    run_id: 'run-public-surface',
    sequence: 1,
    timestamp: '2026-01-01T00:00:00Z',
    type: 'agent_execution_started',
    execution_id: 'exec-1',
    invocation_id: 'exec-1',
    agent_kind: 'implementer',
    round_label: 'round-1-implementer',
  };
}

function outputChunk(sequence: number, content: string): RunEvent {
  return {
    run_id: 'run-public-surface',
    sequence,
    timestamp: `2026-01-01T00:00:0${sequence}Z`,
    type: 'agent_output_chunk',
    execution_id: 'exec-1',
    invocation_id: 'exec-1',
    agent_kind: 'implementer',
    round_label: 'round-1-implementer',
    data: {kind: 'agent_output_chunk', channel: 'assistant', content},
  };
}

function roundFinished(sequence: number): RunEvent {
  return {
    run_id: 'run-public-surface',
    sequence,
    timestamp: `2026-01-01T00:00:0${sequence}Z`,
    type: 'round_finished',
    round_label: 'round-1',
    data: {
      kind: 'round_finished',
      attempts: 1,
      judge_verdict: 'pass',
      profile_skipped: false,
    },
  };
}

function toolCall(sequence: number, arguments_: Record<string, unknown>): RunEvent {
  return {
    ...outputChunk(sequence, ''),
    type: 'tool_call',
    data: {kind: 'tool_call', tool: 'test', args: arguments_},
  };
}

function runningSnapshot(sequence: number): RunSnapshot {
  return {
    run_id: 'run-public-surface',
    sequence,
    status: 'running',
  };
}

function expectPublicationGuardsDisabled(): void {
  const state = coreState.reduceEvent(coreState.initialCoreState(), executionStarted());
  const arguments_ = {items: ['original']};
  const withArguments = coreState.reduceEvent(state, toolCall(2, arguments_));
  const publishedItems = withArguments.transcript[0]?.toolArguments?.['items'];
  expect(Object.isFrozen(state.rounds)).toBe(false);
  expect(Object.isFrozen(state.rounds[0])).toBe(false);
  expect(Object.isFrozen(state.transcript)).toBe(false);
  expect(Array.isArray(publishedItems)).toBe(true);
  expect(publishedItems as unknown).toBe(arguments_.items);
  expect(Object.isFrozen(publishedItems)).toBe(false);
}

function expectFrozenReachableReferences(state: CoreState): void {
  const seen = new WeakSet<object>();
  for (const value of Object.values(state)) expectFrozenReachableValue(value, seen);
}

function expectFrozenReachableValue(value: unknown, seen: WeakSet<object>): void {
  if (typeof value !== 'object' || value === null || seen.has(value)) return;
  seen.add(value);
  expect(Object.isFrozen(value)).toBe(true);
  for (const nested of Object.values(value)) expectFrozenReachableValue(nested, seen);
}

function withNodeEnvironment<T>(nodeEnvironment: string | undefined, run: () => T): T {
  const previous = process.env.NODE_ENV;
  try {
    if (nodeEnvironment === undefined) delete process.env.NODE_ENV;
    else process.env.NODE_ENV = nodeEnvironment;
    return run();
  } finally {
    if (previous === undefined) delete process.env.NODE_ENV;
    else process.env.NODE_ENV = previous;
  }
}

/** Compile-time assertions for consumers of the package root. */
function readonlyContract(
  state: CoreState,
  round: RoundState,
  phase: AgentPhase,
  transcript: TranscriptEntry,
  diagnostic: CoreDiagnostic,
  boundary: RunLifetimeBoundary,
): void {
  // @ts-expect-error published state fields are read-only
  state.sequence = 10;
  // @ts-expect-error published collections cannot be extended
  state.rounds.push(round);
  // @ts-expect-error published projection entries are read-only
  phase.status = 'completed';
  // @ts-expect-error published transcript entries are read-only
  transcript.content = 'changed';
  // @ts-expect-error nested published tool-result entries are read-only
  if (transcript.toolResult !== undefined) transcript.toolResult.content = 'changed';
  if (transcript.toolResult?.payload?.kind === 'command') {
    // @ts-expect-error nested protocol payloads are read-only
    transcript.toolResult.payload.stdout = 'changed';
  }
  const payload = transcript.toolResult?.payload;
  if (payload?.kind === 'json' && Array.isArray(payload.value)) {
    const firstValue = payload.value[0];
    const length = payload.value.length;
    payload.value.forEach(value => void value);
    void firstValue;
    void length;
    // @ts-expect-error Array.isArray must not expose mutable protocol arrays
    payload.value.push('changed');
    // @ts-expect-error all mutable array methods stay unavailable after narrowing
    payload.value.pop();
    // @ts-expect-error all mutable array methods stay unavailable after narrowing
    payload.value.shift();
    // @ts-expect-error all mutable array methods stay unavailable after narrowing
    payload.value.unshift('changed');
    // @ts-expect-error all mutable array methods stay unavailable after narrowing
    payload.value.splice(0, 1);
    // @ts-expect-error all mutable array methods stay unavailable after narrowing
    payload.value.sort();
    // @ts-expect-error all mutable array methods stay unavailable after narrowing
    payload.value.reverse();
    // @ts-expect-error all mutable array methods stay unavailable after narrowing
    payload.value.copyWithin(0, 1);
    // @ts-expect-error all mutable array methods stay unavailable after narrowing
    payload.value.fill(null);
    // @ts-expect-error array indices remain read-only after narrowing
    payload.value[0] = 'changed';
  }
  const nestedArgument = transcript.toolArguments?.['nested'];
  if (
    nestedArgument !== null &&
    typeof nestedArgument === 'object' &&
    !Array.isArray(nestedArgument)
  ) {
    // @ts-expect-error nested JSON objects are read-only
    nestedArgument['value'] = 'changed';
  }
  // @ts-expect-error retained protocol events are read-only projections
  boundary.event.type = 'run_failed';
  if (boundary.event.data?.kind === 'run_started') {
    // @ts-expect-error arrays nested in retained protocol events are read-only
    boundary.event.data.expected_roles?.push('agent');
  }
  // @ts-expect-error published diagnostics are read-only
  diagnostic.summary = 'changed';
}
void readonlyContract;

/** Compile-time guard that publication typing does not constrain mutable inputs. */
function mutableInputContract(event: RunEvent, value: unknown): void {
  if (Array.isArray(value)) value.push('allowed');
  const payload = event.data?.kind === 'tool_result' ? event.data.payload : undefined;
  if (payload?.kind === 'json' && Array.isArray(payload.value)) {
    payload.value.push('allowed');
  }
}
void mutableInputContract;
