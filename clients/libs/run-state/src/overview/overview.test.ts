import {describe, expect, it} from 'bun:test';
import type {RunEvent, RunOverview, RunSnapshot, Span} from '@vibesys/contracts';
import {
  adoption,
  foldOverview,
  type GateReading,
  gateTrajectory,
  overviewFromSnapshot,
  workstreamCounts,
} from './index.js';

/** mulberry32: a seeded generator whose low bits vary (fast-check is not a dependency). */
class Seeded {
  #state: number;

  constructor(seed: number) {
    this.#state = seed >>> 0;
  }

  int(bound: number): number {
    this.#state = (this.#state + 0x6d2b79f5) >>> 0;
    let value = this.#state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) % bound;
  }
}

const STATUSES = ['running', 'waiting', 'pending', 'done', 'failed', 'stopped'] as const;
const GATE_VERDICTS = [null, 'passed', 'failed_check', 'slower'] as const;
const WORKSTREAM_VERDICTS = [null, 'passed', 'failed'] as const;

function overview(text: string): RunOverview {
  return {overview_version: 1, objective: {text, metric: 'throughput'}, benchmarks: {}};
}

function started(sequence: number, declared: RunOverview | null): RunEvent {
  return {
    protocol_version: 1,
    run_id: 'run',
    sequence,
    timestamp: '2026-01-01T00:00:00Z',
    type: 'run_started',
    data: {kind: 'run_started', outer_loop: 'dynamic', input: 'queue', overview: declared},
  } as RunEvent;
}

/** A run's spans: workstreams with statuses and verdicts, each with a gate or none. */
function spans(random: Seeded): Span[] {
  const found: Span[] = [];
  for (let number = 0; number < random.int(8); number += 1) {
    const id = `flow:w${number}/#1`;
    found.push({
      id,
      parent: 'flow:#1',
      type: 'flow',
      name: 'workstream',
      status: STATUSES[random.int(STATUSES.length)] ?? 'running',
      started_at: number,
      label: {kind: 'workstream', verdict: WORKSTREAM_VERDICTS[random.int(3)] ?? null},
    });
    if (random.int(3) === 0) continue;
    const value = random.int(4) === 0 ? null : random.int(1000);
    found.push({
      id: `op:w${number}/measure.1`,
      parent: id,
      type: 'operation',
      name: 'measure',
      status: 'done',
      started_at: number,
      ended_at: number + 1,
      label: {
        kind: 'gate',
        verdict: GATE_VERDICTS[random.int(4)] ?? null,
        attrs_json: JSON.stringify({
          stage: 'benchmark',
          metric_value: value,
          vs_baseline: value && value / 100,
        }),
      },
    });
  }
  return found;
}

describe('run overview', () => {
  it('is the last overview a run_started event declared, from the snapshot onward', () => {
    for (let seed = 1; seed <= 100; seed += 1) {
      const random = new Seeded(seed);
      const declared = Array.from({length: random.int(4)}, (_, n) =>
        random.int(3) === 0 ? null : overview(`objective ${n}`),
      );
      const events = declared.map((item, n) => started(n + 1, item));
      const last = declared.filter(item => item !== null).at(-1) ?? null;
      expect(foldOverview(null, events)).toEqual(last);
      const snapshot = {overview: declared[0] ?? null, sequence: 1} as RunSnapshot;
      expect(foldOverview(overviewFromSnapshot(snapshot), events.slice(1))).toEqual(
        declared
          .slice(1)
          .filter(item => item !== null)
          .at(-1) ??
          declared[0] ??
          null,
      );
    }
  });

  it('counts workstreams and picks the best passed gate the way a direct reading does', () => {
    for (let seed = 1; seed <= 200; seed += 1) {
      const run = spans(new Seeded(seed));
      const workstreams = run.filter(span => span.label?.kind === 'workstream');
      const counts = workstreamCounts(run);
      expect(counts.total).toBe(workstreams.length);
      expect(counts.live).toBe(workstreams.filter(span => span.status === 'running').length);
      expect(counts.passed + counts.failed).toBeLessThanOrEqual(counts.total);
      expect(counts.live + counts.waiting).toBeLessThanOrEqual(counts.total);

      const gates = gateTrajectory(run);
      expect(gates.map((gate: GateReading) => gate.span)).toEqual(
        run.filter(span => span.label?.kind === 'gate').map(span => span.id),
      );
    }
  });

  it('reads nothing from a run whose search labels no spans', () => {
    const plain: Span = {
      id: 'flow:#1',
      parent: null,
      type: 'flow',
      name: 'root',
      status: 'running',
      started_at: 0,
    };
    expect(gateTrajectory([plain])).toEqual([]);
    expect(workstreamCounts([plain]).total).toBe(0);
  });
});

describe('adoption', () => {
  const adoptionSpan = (label: NonNullable<Span['label']> | undefined, id = 'op:adopt.1'): Span =>
    ({
      id,
      parent: 'flow:#1',
      type: 'operation',
      name: 'propose_winner',
      status: 'done',
      started_at: 1,
      label,
    }) as Span;
  const labelled = (verdict: string | null, attrs: object): NonNullable<Span['label']> => ({
    kind: 'adoption',
    verdict,
    attrs_json: JSON.stringify(attrs),
  });

  it('is null for a run that has not begun to adopt, or whose search labels no spans', () => {
    expect(adoption([])).toBeNull();
    expect(adoption([adoptionSpan(undefined)])).toBeNull();
  });

  it('reads the candidate, what it measured, its ratio and the verdict', () => {
    const span = adoptionSpan(
      labelled('adopted', {candidate: 'c2', metric_value: 150, vs_baseline: 1.5}),
    );
    expect(adoption([span])).toEqual({
      span: 'op:adopt.1',
      candidate: 'c2',
      metricValue: 150,
      vsBaseline: 1.5,
      verdict: 'adopted',
    });
  });

  it('reads an adoption of the input, still adopting, or with unknown values as nulls', () => {
    const adopted = adoption([
      adoptionSpan(labelled(null, {candidate: null, metric_value: null, vs_baseline: null})),
    ]);
    expect(adopted).toMatchObject({
      candidate: null,
      metricValue: null,
      vsBaseline: null,
      verdict: null,
    });
  });

  it('is the latest adoption when a restart proposed the winner again', () => {
    const first = adoptionSpan(labelled('not_adopted', {candidate: 'c1'}), 'op:adopt.1');
    const second = adoptionSpan(labelled('adopted', {candidate: 'c2'}), 'op:adopt.2');
    expect(adoption([first, second])?.candidate).toBe('c2');
  });
});
