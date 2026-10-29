import {describe, expect, it} from 'bun:test';
import type {RunEvent, RunSnapshot} from '@vibesys/contracts';
import type {CoreDiagnostic, CoreState, RunLifetimeBoundary} from './index.js';
import * as fold from './index.js';
import * as performance from './performance/index.js';
import type {AgentPhase, RoundState} from './rounds/index.js';
import * as rounds from './rounds/index.js';
import type {TranscriptEntry} from './transcript/index.js';
import * as transcript from './transcript/index.js';

describe('published run-state surface', () => {
  it('exports only the fold, its read helpers, and each projection from its own entry', () => {
    expect(Object.keys(fold).sort()).toEqual([
      'EMPTY_LEDGER',
      'NO_TOKENS',
      'addTokens',
      'executionStatusFor',
      'foldEventBatch',
      'foldEventRebootstrap',
      'foldResponseEvents',
      'foldSnapshot',
      'hasRunEnded',
      'initialCoreState',
      'initialIterationNavigation',
      'iterationChildren',
      'iterationRoots',
      'iterationRootsForCore',
      'latestDiagnosticChange',
      'reconcileActiveExecutions',
      'reduceEvent',
      'reduceEventBatch',
      'reduceEventPrefix',
      'reduceEventRebootstrap',
      'reduceIterationResult',
      'reduceResponseEvents',
      'reduceSnapshot',
      'rehydrateCoreState',
      'totalTokens',
    ]);
    expect(Object.keys(rounds).sort()).toEqual([
      'activeRunFocus',
      'agentKindText',
      'describePhase',
      'experimentForRound',
      'hasActiveAgentTiming',
      'hypothesisRoundNumbers',
      'joinRoundsWithExperiments',
      'phaseText',
      'phasesForRound',
      'planningStageForPhase',
      'roundAgentElapsedMs',
      'roundKeyFor',
      'roundOutcome',
      'roundsWithPlan',
      'sameRoundKey',
    ]);
    expect(Object.keys(performance).sort()).toEqual([
      'formatPerformanceDelta',
      'projectPerformance',
      'recordsBenchmark',
    ]);
    expect(Object.keys(transcript).sort()).toEqual([
      'DEFAULT_CHAT_THREAD_ID',
      'appendNativeTranscript',
      'emptyNativeTranscript',
      'nativeTranscriptEntries',
      'nativeTranscriptHashes',
      'transcriptBySpan',
    ]);
  });

  it('rejects writes throughout a development projection without corrupting another snapshot', () => {
    withNodeEnvironment('development', () => {
      let first = fold.reduceEvent(fold.initialCoreState(), executionStarted());
      first = fold.reduceEvent(first, outputChunk(2, 'hello'));
      const second = fold.reduceSnapshot(first, runningSnapshot(3));

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
      const appended = fold.reduceEvent({...second}, outputChunk(4, ' world'));
      const continued = fold.reduceEvent(appended, roundFinished(5));
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
      const first = fold.reduceEvent(fold.initialCoreState(), toolCall(1, arguments_));
      const second = fold.reduceSnapshot(first, runningSnapshot(2));
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

  it('keeps unsequenced transcript evidence in its agent scope with unique identities', () => {
    for (const sequence of [undefined, 0] as const) {
      const first = unsequencedOutput({
        sequence,
        timestamp: '2026-01-01T00:00:00Z',
        agentKind: 'implementer',
        roundLabel: 'round-1-implementer',
        content: 'implementation evidence',
      });
      const second = unsequencedOutput({
        sequence,
        timestamp: sequence === 0 ? '2026-01-01T00:00:01Z' : first.timestamp,
        agentKind: 'judge',
        roundLabel: 'round-1-judge',
        content: 'judge evidence',
      });

      const state = fold.reduceEventBatch(fold.initialCoreState(), [first, second]);

      expect(
        state.transcript.map(entry => ({
          content: entry.content,
          agentKind: entry.agentKind,
          roundLabel: entry.roundLabel,
        })),
      ).toEqual([
        {
          content: 'implementation evidence',
          agentKind: 'implementer',
          roundLabel: 'round-1-implementer',
        },
        {content: 'judge evidence', agentKind: 'judge', roundLabel: 'round-1-judge'},
      ]);
      expect(new Set(state.transcript.map(entry => entry.id)).size).toBe(state.transcript.length);
    }
  });

  it('coalesces one legacy turn but gives distinct unsequenced cards distinct identities', () => {
    const chunk = {
      sequence: 0 as const,
      timestamp: '2026-01-01T00:00:00Z',
      agentKind: 'implementer',
      roundLabel: 'round-1-implementer',
    };
    const withTurn = fold.reduceEventBatch(fold.initialCoreState(), [
      unsequencedOutput({...chunk, content: 'one '}),
      unsequencedOutput({...chunk, timestamp: '2026-01-01T00:00:01Z', content: 'turn'}),
    ]);
    expect(withTurn.transcript.map(entry => entry.content)).toEqual(['one turn']);

    const withCards = fold.reduceEventBatch(withTurn, [
      unsequencedPhase(chunk),
      unsequencedPhase(chunk),
    ]);
    expect(withCards.transcript.map(entry => entry.content)).toEqual([
      'one turn',
      'started',
      'started',
    ]);
    expect(new Set(withCards.transcript.map(entry => entry.id)).size).toBe(
      withCards.transcript.length,
    );
  });

  it('keeps an unsequenced event key stable when an older prefix is backfilled', () => {
    const scope = {
      sequence: 0 as const,
      timestamp: '2026-01-01T00:00:00Z',
      agentKind: 'implementer',
      roundLabel: 'round-1-implementer',
    };
    const newer = unsequencedOutput({...scope, content: 'newer evidence'});
    const tail = fold.reduceEventBatch(fold.initialCoreState(), [newer]);
    const newerId = tail.transcript[0]?.id;

    const merged = fold.reduceEventPrefix(
      tail,
      [unsequencedOutput({...scope, content: 'older evidence'}), unsequencedPhase(scope)],
      0,
    );

    expect(merged.transcript.find(entry => entry.content === 'newer evidence')?.id).toBe(newerId);
    expect(new Set(merged.transcript.map(entry => entry.id)).size).toBe(merged.transcript.length);
  });

  it('keeps unsequenced presentation identities bounded for maximum-size payloads', () => {
    const content = 'x'.repeat(1_000_000);
    const state = fold.reduceEvent(
      fold.initialCoreState(),
      unsequencedOutput({
        sequence: 0,
        timestamp: '2026-01-01T00:00:00Z',
        agentKind: 'implementer',
        roundLabel: 'round-1-implementer',
        content,
      }),
    );

    expect(state.transcript[0]?.content).toHaveLength(content.length);
    expect(state.transcript[0]?.id.length).toBeLessThan(100);
  });
});

interface UnsequencedOutput {
  readonly sequence: 0 | undefined;
  readonly timestamp: string;
  readonly agentKind: string;
  readonly roundLabel: string;
  readonly content: string;
}

function unsequencedOutput({
  sequence,
  timestamp,
  agentKind,
  roundLabel,
  content,
}: UnsequencedOutput): RunEvent {
  return {
    ...(sequence === undefined ? {} : {sequence}),
    timestamp,
    type: 'agent_output_chunk',
    agent_kind: agentKind,
    round_label: roundLabel,
    data: {kind: 'agent_output_chunk', channel: 'assistant', content},
  };
}

function unsequencedPhase({
  sequence,
  timestamp,
  agentKind,
  roundLabel,
}: Omit<UnsequencedOutput, 'content'>): RunEvent {
  return {
    ...(sequence === undefined ? {} : {sequence}),
    timestamp,
    type: 'phase_started',
    agent_kind: agentKind,
    round_label: roundLabel,
  };
}

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
  const state = fold.reduceEvent(fold.initialCoreState(), executionStarted());
  const arguments_ = {items: ['original']};
  const withArguments = fold.reduceEvent(state, toolCall(2, arguments_));
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
