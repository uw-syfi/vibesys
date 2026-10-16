import {describe, expect, it} from 'bun:test';
import type {RunEvent, RunOverview, RunSnapshot, Span} from '@vibesys/contracts';
import {
  adoption,
  bestVsBaseline,
  foldOverview,
  type GateReading,
  gateTrajectory,
  overviewFromSnapshot,
  runningBest,
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
const WORKSTREAM_VERDICTS = [null, 'accepted', 'rejected'] as const;

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

/** The passed gates that beat every passed gate before them, read directly. */
function improvements(run: readonly Span[], direction: 'max' | 'min'): string[] {
  let best: number | null = null;
  const found: string[] = [];
  for (const gate of gateTrajectory(run)) {
    if (gate.verdict !== 'passed' || gate.metricValue === null) continue;
    const value = gate.metricValue;
    if (best !== null && (direction === 'max' ? value <= best : value >= best)) continue;
    best = value;
    found.push(gate.span);
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
      expect(counts.accepted + counts.rejected).toBeLessThanOrEqual(counts.total);
      expect(counts.live + counts.waiting).toBeLessThanOrEqual(counts.total);

      const gates = gateTrajectory(run);
      expect(gates.map((gate: GateReading) => gate.span)).toEqual(
        run.filter(span => span.label?.kind === 'gate').map(span => span.id),
      );
      const passed = gates.filter(
        (g: GateReading) => g.verdict === 'passed' && g.metricValue !== null,
      );
      const values = passed.map((g: GateReading) => g.metricValue as number);
      expect(bestVsBaseline(run, 'max')?.metricValue ?? null).toBe(
        values.length ? Math.max(...values) : null,
      );
      expect(bestVsBaseline(run, 'min')?.metricValue ?? null).toBe(
        values.length ? Math.min(...values) : null,
      );
    }
  });

  it('steps the best so far through passed gates only, ending at the best', () => {
    for (let seed = 1; seed <= 200; seed += 1) {
      const run = spans(new Seeded(seed));
      for (const direction of ['max', 'min'] as const) {
        const steps = runningBest(run, direction);
        expect(steps.at(-1) ?? null).toEqual(bestVsBaseline(run, direction));
        expect(steps.map(step => step.span)).toEqual(improvements(run, direction));
        const values = steps.map(step => step.metricValue as number);
        const better = (a: number, b: number) => (direction === 'max' ? a > b : a < b);
        expect(values.every((value, n) => n === 0 || better(value, values[n - 1] as number))).toBe(
          true,
        );
      }
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
    expect(bestVsBaseline([plain], 'max')).toBeNull();
    expect(runningBest([plain], 'max')).toEqual([]);
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
