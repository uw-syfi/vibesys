import type {RunEvent} from '@vibesys/backend-client';
import type {CoreState} from '../src/core-state.js';
import type {RunMapState} from '../src/run-map.js';

const runMapModule =
  Bun.env.RUN_MAP_BENCH_MODULE ?? new URL('../src/run-map.ts', import.meta.url).pathname;
const {applyRunMapEvent} = (await import(runMapModule)) as typeof import('../src/run-map.js');
const coreStateModule =
  Bun.env.RUN_MAP_BENCH_CORE_MODULE ?? new URL('../src/core-state.ts', import.meta.url).pathname;
const {initialCoreState, reduceEvent, reduceEventBatch} = (await import(
  coreStateModule
)) as typeof import('../src/core-state.js');

const ROLES = ['orchestrator', 'implementer', 'judge', 'profiler'] as const;
const LIVE_SAMPLES = 5_000;

interface Measurement {
  readonly kind:
    | 'run-map replay'
    | 'core replay'
    | 'run-map live no-op'
    | 'run-map phase replacement'
    | 'core live no-op'
    | 'core phase replacement';
  readonly scale: number;
  readonly phases: number;
  readonly totalMs: number;
  readonly microsecondsPerEvent: number;
}

const measurements: Measurement[] = [];

for (const count of [15_000, 36_000, 72_000]) {
  const input = replayEvents(count, 280);
  let state = emptyRunMap();
  const startedAt = performance.now();
  for (const event of input) state = applyRunMapEvent(state, event);
  measurements.push(
    measurement('run-map replay', count, state, performance.now() - startedAt, count),
  );

  const coreStartedAt = performance.now();
  const coreState = reduceEventBatch(initialCoreState(), input);
  measurements.push(
    measurement('core replay', count, coreState, performance.now() - coreStartedAt, count),
  );
}

const outputCosts: Measurement[] = [];
const phaseCosts: Measurement[] = [];
for (const round of [20, 100, 280, 2_000]) {
  const base = replay(replayEvents(round * 40, round));
  outputCosts.push(measureLiveOutput(base, round));
  phaseCosts.push(measureLivePhaseReplacement(base, round));
}
measurements.push(...outputCosts, ...phaseCosts);

const coreOutputCosts: Measurement[] = [];
const corePhaseCosts: Measurement[] = [];
for (const round of [20, 100, 280, 2_000]) {
  const base = reduceEventBatch(initialCoreState(), replayEvents(round * 40, round));
  coreOutputCosts.push(measureCoreNoop(base, round));
  corePhaseCosts.push(measureCorePhaseReplacement(base, round));
}
measurements.push(...coreOutputCosts, ...corePhaseCosts);

console.log('| Path | Events / late round | Phases | Total ms | µs/event |');
console.log('|---|---:|---:|---:|---:|');
for (const result of measurements) {
  console.log(
    `| ${result.kind} | ${result.scale} | ${result.phases} | ${result.totalMs.toFixed(2)} | ${result.microsecondsPerEvent.toFixed(3)} |`,
  );
}

assertNearFlat('live output', outputCosts);
assertNearFlat('live phase replacement', phaseCosts);
assertNearFlat('core live no-op', coreOutputCosts);
assertNearFlat('core phase replacement', corePhaseCosts);

function measureLiveOutput(base: RunMapState, round: number): Measurement {
  let state = base;
  const startedAt = performance.now();
  for (let sample = 0; sample < LIVE_SAMPLES; sample += 1) {
    state = applyRunMapEvent(
      state,
      scopedEvent(100_000 + sample, round, 'implementer', 'agent_output_chunk'),
    );
  }
  return measurement(
    'run-map live no-op',
    round,
    state,
    performance.now() - startedAt,
    LIVE_SAMPLES,
  );
}

function measureLivePhaseReplacement(base: RunMapState, round: number): Measurement {
  let state = applyRunMapEvent(base, {
    ...scopedEvent(90_000, round, 'implementer', 'phase_started'),
    execution_id: 'live-phase',
    invocation_id: 'live-phase',
  });
  const startedAt = performance.now();
  for (let sample = 0; sample < LIVE_SAMPLES; sample += 1) {
    state = applyRunMapEvent(state, {
      ...scopedEvent(100_000 + sample, round, 'implementer', 'phase_started'),
      execution_id: 'live-phase',
      invocation_id: 'live-phase',
    });
  }
  return measurement(
    'run-map phase replacement',
    round,
    state,
    performance.now() - startedAt,
    LIVE_SAMPLES,
  );
}

function measureCoreNoop(base: CoreState, round: number): Measurement {
  let state = base;
  const startedAt = performance.now();
  for (let sample = 0; sample < LIVE_SAMPLES; sample += 1) {
    state = reduceEvent(
      state,
      scopedEvent(100_000 + sample, round, 'implementer', 'agent_execution_activity_changed'),
    );
  }
  return measurement('core live no-op', round, state, performance.now() - startedAt, LIVE_SAMPLES);
}

function measureCorePhaseReplacement(base: CoreState, round: number): Measurement {
  let state = reduceEvent(base, {
    ...scopedEvent(90_000, round, 'implementer', 'agent_execution_started'),
    execution_id: 'live-phase',
    invocation_id: 'live-phase',
  });
  const startedAt = performance.now();
  for (let sample = 0; sample < LIVE_SAMPLES; sample += 1) {
    state = reduceEvent(state, {
      ...scopedEvent(100_000 + sample, round, 'implementer', 'agent_execution_started'),
      execution_id: 'live-phase',
      invocation_id: 'live-phase',
    });
  }
  return measurement(
    'core phase replacement',
    round,
    state,
    performance.now() - startedAt,
    LIVE_SAMPLES,
  );
}

function measurement(
  kind: Measurement['kind'],
  scale: number,
  state: RunMapState | CoreState,
  totalMs: number,
  events: number,
): Measurement {
  return {
    kind,
    scale,
    phases: state.phases.length,
    totalMs,
    microsecondsPerEvent: (totalMs * 1_000) / events,
  };
}

function assertNearFlat(kind: string, results: readonly Measurement[]): void {
  const costs = results.map(result => result.microsecondsPerEvent);
  const ratio = Math.max(...costs) / Math.min(...costs);
  if (ratio > 2.5) {
    throw new Error(`${kind} cost grew ${ratio.toFixed(2)}x across late-round scales`);
  }
}

function replay(events: readonly RunEvent[]): RunMapState {
  let state = emptyRunMap();
  for (const event of events) state = applyRunMapEvent(state, event);
  return state;
}

function replayEvents(count: number, rounds: number): RunEvent[] {
  const events: RunEvent[] = [runStarted(rounds)];
  const ordinaryEvents = count - 1;
  const perRound = Math.floor(ordinaryEvents / rounds);
  let remainder = ordinaryEvents % rounds;
  let sequence = 2;
  for (let round = 1; round <= rounds; round += 1) {
    const eventsThisRound = perRound + (remainder > 0 ? 1 : 0);
    remainder -= remainder > 0 ? 1 : 0;
    for (const role of ROLES) {
      events.push(scopedEvent(sequence, round, role, 'agent_execution_started'));
      sequence += 1;
    }
    const outputCount = Math.max(0, eventsThisRound - ROLES.length * 2);
    for (let offset = 0; offset < outputCount; offset += 1) {
      const role = ROLES[offset % ROLES.length] ?? 'implementer';
      events.push(scopedEvent(sequence, round, role, 'agent_output_chunk'));
      sequence += 1;
    }
    for (const role of ROLES) {
      events.push(scopedEvent(sequence, round, role, 'agent_execution_finished'));
      sequence += 1;
    }
  }
  return events.slice(0, count);
}

function runStarted(rounds: number): RunEvent {
  return {
    sequence: 1,
    timestamp: timestamp(1),
    type: 'run_started',
    status: 'active',
    data: {
      kind: 'run_started',
      outer_loop: 'agent',
      input: '/benchmark',
      max_rounds: rounds,
      expected_roles: [...ROLES],
    },
  };
}

function scopedEvent(
  sequence: number,
  round: number,
  role: string,
  type: RunEvent['type'],
): RunEvent {
  const executionId = `${round}-${role}`;
  return {
    sequence,
    timestamp: timestamp(sequence),
    type,
    status: type.endsWith('_finished') ? 'completed' : 'active',
    agent_kind: role,
    round_label: `round-${round}-${role}`,
    execution_id: executionId,
    invocation_id: executionId,
  };
}

function timestamp(sequence: number): string {
  return new Date(1_767_225_600_000 + sequence).toISOString();
}

function emptyRunMap(): RunMapState {
  return {
    outerLoop: null,
    expectedRoles: null,
    rounds: [],
    phases: [],
    lastEventTimestamp: null,
  };
}
