import {describe, expect, test} from 'bun:test';
import type {RunEvent, RunOverview, RunSnapshot, Span} from '@vibesys/contracts';
import {bestVsBaseline, runningBest, workstreamCounts} from '@vibesys/run-state/overview';
import {EMPTY_TRACE, foldTrace, traceFromSnapshot} from '@vibesys/run-state/trace';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  formatValue,
  type OverviewView,
  objectiveBlocks,
  overviewView,
  type RunAtView,
  type RunPageState,
  type RunRecord,
  runPageInitial,
  WORK_OUTCOMES,
  type WorkFilter,
  type WorkView,
  workView,
} from './index.js';

/**
 * The Overview and Work selectors over generated runs: span events that start, update and end
 * planning calls, workstreams (with builds-on links and verdicts), agent turns and gates (some
 * under an unlabelled flow inside their workstream), spans of a kind this client does not know,
 * and unlabelled flows. At every sequence the selectors agree with a direct reading of the spans
 * at that sequence, and at the last one they equal the selectors over the live trace.
 */

const STATUSES = ['pending', 'running', 'waiting', 'done', 'failed', 'stopped'] as const;
const OPEN = new Set(['pending', 'running', 'waiting']);
const GATE_VERDICTS = [null, 'passed', 'slower', 'failed_check'] as const;
const WORK_VERDICTS = [null, 'accepted', 'rejected'] as const;
const ROLES = ['implementer', 'reviewer', 'planner'] as const;

function event(sequence: number, type: string, span: Span): RunEvent {
  return {
    protocol_version: 1,
    run_id: 'run',
    sequence,
    timestamp: '2026-10-15T16:30:00Z',
    type,
    data: {kind: type, span},
  } as RunEvent;
}

interface Generated {
  readonly events: readonly RunEvent[];
  readonly direction: 'max' | 'min';
}

/** Writes a generated run's span events, each carrying the whole span, as the runtime emits them. */
class RunWriter {
  readonly events: RunEvent[] = [];
  readonly #current = new Map<string, Span>();
  readonly #random: Seeded;
  readonly #labelled: boolean;
  #clock = 100;

  constructor(random: Seeded, labelled: boolean) {
    this.#random = random;
    this.#labelled = labelled;
  }

  emit(span: Span): void {
    const known = this.#current.has(span.id);
    const type = !known ? 'span_started' : OPEN.has(span.status) ? 'span_updated' : 'span_ended';
    this.#current.set(span.id, span);
    this.events.push(event(this.events.length + 1, type, span));
  }

  tick(): number {
    this.#clock += 1 + this.#random.int(90);
    return this.#clock;
  }

  label(kind: string, extra: Partial<NonNullable<Span['label']>> = {}): Partial<Span> {
    return this.#labelled ? {label: {kind, ...extra}} : {};
  }

  planningCall(call: number): void {
    const planned: Span = {
      id: `plan${call}`,
      parent: 'root',
      type: 'flow',
      name: 'plan',
      status: 'running',
      started_at: this.tick(),
      ...this.label('planning_call', {
        title: `planning call ${call}`,
        attrs_json: `{"call":${call}}`,
      }),
    };
    this.emit(planned);
    if (this.#random.int(4) > 0) this.emit({...planned, status: 'done', ended_at: this.tick()});
  }

  /** A workstream, its turns and gate (directly under it or under an unlabelled flow), and its end. */
  workstream(id: string, earlier: readonly string[]): void {
    const random = this.#random;
    const links =
      earlier.length > 0 && random.bool() ? [{rel: 'builds_on', to: random.pick(earlier)}] : [];
    const work: Span = {
      id,
      parent: 'root',
      type: 'flow',
      name: 'workstream',
      status: random.bool() ? 'running' : 'pending',
      started_at: this.tick(),
      ...this.label('workstream', {
        title: random.pick(['Lock-free ring', 'Cache padding', 'Batch enqueue', '']),
        attrs_json: JSON.stringify({hypothesis_id: `H${id}`, parent_revision: 'r0'}),
        links,
      }),
    };
    this.emit(work);
    const holder = random.bool() ? id : `${id}/inner`;
    if (holder !== id) {
      this.emit({
        id: holder,
        parent: id,
        type: 'flow',
        name: 'work',
        status: 'running',
        started_at: this.tick(),
      });
    }
    for (let t = 0; t < random.int(4); t += 1) this.turn(`${id}/turn${t}`, holder);
    if (random.int(3) > 0) this.gate(`${id}/gate`, holder);
    if (random.int(3) === 0) {
      const odd = this.label('profile_probe', {attrs_json: '[1,2'});
      this.emit({
        id: `${id}/odd`,
        parent: id,
        type: 'flow',
        name: 'odd',
        status: 'running',
        started_at: this.tick(),
        ...odd,
      });
    }
    this.finish(work);
  }

  turn(id: string, parent: string): void {
    const turn: Span = {
      id,
      parent,
      type: 'operation',
      name: 'ask',
      status: 'running',
      started_at: this.tick(),
      ...this.label('agent_turn', {attrs_json: JSON.stringify({role: this.#random.pick(ROLES)})}),
    };
    this.emit(turn);
    if (this.#random.int(3) > 0) this.emit({...turn, status: 'done', ended_at: this.tick()});
  }

  gate(id: string, parent: string): void {
    const random = this.#random;
    const gate: Span = {
      id,
      parent,
      type: 'operation',
      name: 'measure',
      status: 'running',
      started_at: this.tick(),
      ...this.label('gate', {title: 'benchmark', attrs_json: '{"stage":"benchmark"}'}),
    };
    this.emit(gate);
    if (random.int(4) === 0) return;
    const value = random.int(5) === 0 ? null : 50 + random.int(100);
    const attrs = {
      stage: 'benchmark',
      metric_value: value,
      vs_baseline: value === null ? null : value / 80,
    };
    this.emit({
      ...gate,
      status: 'done',
      ended_at: this.tick(),
      ...this.label('gate', {
        title: 'benchmark',
        attrs_json: JSON.stringify(attrs),
        verdict: random.pick(GATE_VERDICTS),
      }),
    });
  }

  finish(work: Span): void {
    const last = this.#random.pick(STATUSES);
    const open = OPEN.has(last);
    const verdict = open ? null : this.#random.pick(WORK_VERDICTS);
    this.emit({
      ...work,
      status: last,
      ...(open ? {} : {ended_at: this.tick()}),
      ...(work.label ? {label: {...work.label, verdict}} : {}),
      ...(last === 'waiting'
        ? {waiting_on: [{kind: 'slot', ref: 'slots', since: this.#clock}]}
        : {}),
    });
  }
}

/** A run's span events, in sequence order: planning calls, each followed by its workstreams. */
function generate(random: Seeded, labelled = true): Generated {
  const writer = new RunWriter(random, labelled);
  writer.emit({
    id: 'root',
    parent: null,
    type: 'flow',
    name: 'dynamic',
    status: 'running',
    started_at: 0,
  });
  const workstreams: string[] = [];
  const calls = 1 + random.int(3);
  for (let call = 1; call <= calls; call += 1) {
    writer.planningCall(call);
    for (let n = 0; n < random.int(4); n += 1) {
      const id = `w${workstreams.length + 1}`;
      writer.workstream(id, workstreams);
      workstreams.push(id);
    }
  }
  return {events: writer.events, direction: random.bool() ? 'max' : 'min'};
}

function declared(direction: 'max' | 'min'): RunOverview {
  return {
    overview_version: 1,
    objective: {
      text: '# Queue\n\nMaximize **throughput**.\n\n- pass the check\n- stay lock-free',
      metric: 'total_ops_per_sec',
      unit: 'ops/s',
      direction,
      correctness_check: ['vibesys-queue', 'check'],
    },
    benchmarks: {evaluator: {name: 'vibesys-evaluator-queue', version: '0.2.0'}},
  };
}

/** The spans at `sequence`, read directly: the last whole span each event carried, in start order. */
function direct(events: readonly RunEvent[], sequence: number): Span[] {
  const spans = new Map<string, Span>();
  for (const item of events) {
    if ((item.sequence ?? 0) > sequence) break;
    const data = item.data as {span: Span};
    spans.set(data.span.id, data.span);
  }
  return [...spans.values()];
}

function atView(
  events: readonly RunEvent[],
  sequence: number,
  overview: RunOverview | null,
): RunAtView {
  const upTo = events.filter(item => (item.sequence ?? 0) <= sequence);
  return {trace: foldTrace(EMPTY_TRACE, upTo), overview};
}

const LIVE_RECORD: RunRecord = {
  kind: 'live',
  run: {
    id: 'a1b2c3d4e5f6',
    projectRoot: '~/vibesys-projects/queue-rs',
    runId: 'run',
    status: 'running',
    phase: 'running',
    paused: null,
    end: null,
    startedAt: 1,
    reason: null,
  },
};

function page(): RunPageState {
  return {
    ...runPageInitial({
      identity: {host: 'ssh:chelan4', runId: 'run'},
      title: {project: 'queue-rs', task: 'mpmc'},
      hostLabel: 'chelan4',
      unavailable: {},
    }),
    record: LIVE_RECORD,
  };
}

const ALL: WorkFilter = {query: '', outcome: 'all'};

function sequences(events: readonly RunEvent[], random: Seeded): number[] {
  const last = events.length;
  return [0, 1, last, ...Array.from({length: 4}, () => random.int(last + 1))];
}

/** The Work rows agree with the workstream spans, read directly. */
function checkWorkRows(spans: readonly Span[], view: WorkView): void {
  expect(view.labelled).toBe(spans.some(span => span.label));
  if (!view.labelled) return;
  const workstreams = spans.filter(span => span.label?.kind === 'workstream');
  expect(view.rows.map(row => row.key)).toEqual(workstreams.map(span => span.id));
  expect(view.total).toBe(workstreams.length);
  const counts = workstreamCounts(spans);
  const by = (kind: string) => view.rows.filter(row => row.outcome.kind === kind).length;
  expect(by('accepted')).toBe(counts.accepted);
  expect(by('rejected')).toBe(counts.rejected);
  expect(by('running')).toBe(
    workstreams.filter(span => OPEN.has(span.status) && !span.label?.verdict).length,
  );
  for (const [index, row] of view.rows.entries()) {
    const span = workstreams[index] as Span;
    const links = (span.label?.links ?? []).map(link => `H${link.to}`);
    expect(row.buildsOn).toBe(links.length === 0 ? 'input' : links.join(', '));
    const turns = spans.filter(
      other => other.label?.kind === 'agent_turn' && other.id.startsWith(`${span.id}/`),
    );
    expect(row.turns === '–' ? 0 : Number(row.turns.split(' ')[0])).toBe(turns.length);
  }
}

function bestText(spans: readonly Span[], direction: 'max' | 'min'): string {
  const best = bestVsBaseline(spans, direction);
  if (best === null) return '–';
  if (best.vsBaseline === null) return `${formatValue(best.metricValue as number)} ops/s`;
  return `${best.vsBaseline.toFixed(2)}×`;
}

/** The key numbers agree with the spans, read directly. */
function checkNumbers(spans: readonly Span[], view: OverviewView, direction: 'max' | 'min'): void {
  const [best, round, workstreams] = view.numbers;
  expect(best?.value).toBe(bestText(spans, direction));
  const calls = spans.filter(span => span.label?.kind === 'planning_call').length;
  expect(round?.value).toBe(calls === 0 ? '–' : String(calls));
  const counts = workstreamCounts(spans);
  expect(workstreams?.value).toBe(String(counts.total));
  expect(workstreams?.sub).toBe(`${counts.live} live · ${counts.waiting} waiting`);
}

/** The chart plots every measured gate and steps through the best so far up to the playhead. */
function checkChart(spans: readonly Span[], view: OverviewView, direction: 'max' | 'min'): void {
  const measured = spans.filter(
    span =>
      span.label?.kind === 'gate' &&
      JSON.parse(span.label?.attrs_json ?? '{}').metric_value != null,
  );
  expect(view.chart?.points.length ?? 0).toBe(measured.length);
  if (view.chart === null) return;
  const steps = runningBest(spans, direction);
  expect(view.chart.steps.length).toBe(steps.length === 0 ? 0 : steps.length + 1);
  expect(view.chart.steps.at(-1)?.y ?? null).toBe(steps.at(-1)?.metricValue ?? null);
  const xs = view.chart.points.map(point => point.x);
  expect(Math.max(...xs)).toBeLessThanOrEqual(view.chart.playhead);
}

describe('the Work tab', () => {
  test('lists every workstream at the view, read as the spans say', () => {
    for (const seed of SEEDS.slice(0, 150)) {
      const random = new Seeded(seed);
      const {events} = generate(random);
      for (const at of sequences(events, random)) {
        checkWorkRows(direct(events, at), workView(atView(events, at, null), ALL));
      }
    }
  });

  test('filters by outcome and query without losing or inventing a row', () => {
    for (const seed of SEEDS.slice(0, 150)) {
      const random = new Seeded(seed);
      const {events} = generate(random);
      const run = atView(events, events.length, null);
      const all = workView(run, ALL).rows;
      const shown = WORK_OUTCOMES.filter(outcome => outcome !== 'all').flatMap(
        outcome => workView(run, {query: '', outcome}).rows,
      );
      for (const row of shown) expect(all).toContainEqual(row);
      expect(shown.length).toBe(
        all.filter(row => ['accepted', 'rejected', 'running'].includes(row.outcome.kind)).length,
      );
      const query = random.pick(['ring', 'PAD', 'Hw1', 'input', 'zzz', ' ']);
      const matched = workView(run, {query, outcome: 'all'}).rows;
      const needle = query.trim().toLowerCase();
      expect(matched).toEqual(
        all.filter(row =>
          [row.id, row.hypothesis, row.buildsOn].some(text => text.toLowerCase().includes(needle)),
        ),
      );
    }
  });

  test('lists an unlabelled run’s flows plainly', () => {
    for (const seed of SEEDS.slice(0, 50)) {
      const {events} = generate(new Seeded(seed), false);
      const spans = direct(events, events.length);
      const view = workView(atView(events, events.length, null), ALL);
      expect(view.labelled).toBe(false);
      expect(view.rows.map(row => row.key)).toEqual(
        spans.filter(span => span.type === 'flow' && span.parent !== null).map(span => span.id),
      );
      for (const row of view.rows) expect(row.id).toBe(row.hypothesis);
    }
  });
});

describe('the Overview tab', () => {
  test('reads its key numbers, chart and current work from the spans at the view', () => {
    for (const seed of SEEDS.slice(0, 150)) {
      const random = new Seeded(seed);
      const {events, direction} = generate(random);
      const overview = declared(direction);
      for (const at of sequences(events, random)) {
        const spans = direct(events, at);
        const view = overviewView(atView(events, at, overview), page());
        checkNumbers(spans, view, direction);
        checkChart(spans, view, direction);
        const open = spans.filter(
          span =>
            ['planning_call', 'workstream'].includes(span.label?.kind ?? '') &&
            OPEN.has(span.status),
        );
        expect(view.current.map(item => item.id)).toEqual(open.map(span => span.id));
      }
    }
  });

  test('at the last sequence equals the view over the live trace', () => {
    for (const seed of SEEDS.slice(0, 100)) {
      const random = new Seeded(seed);
      const {events, direction} = generate(random, random.int(4) > 0);
      const overview = declared(direction);
      const last = events.length;
      // The live trace: a snapshot partway, then the events after it, as the connection folds them.
      const cut = random.int(last + 1);
      const snapshot = {sequence: cut, spans: direct(events, cut)} as RunSnapshot;
      const live: RunAtView = {
        trace: foldTrace(traceFromSnapshot(snapshot), events.slice(cut)),
        overview,
      };
      const viewed = atView(events, last, overview);
      expect(overviewView(viewed, page())).toEqual(overviewView(live, page()));
      for (const outcome of WORK_OUTCOMES) {
        expect(workView(viewed, {query: '', outcome})).toEqual(
          workView(live, {query: '', outcome}),
        );
      }
    }
  });

  test('shows the objective and nothing measured before any span', () => {
    const view = overviewView({trace: EMPTY_TRACE, overview: declared('max')}, page());
    expect(view.objective).toEqual({
      blocks: [
        {kind: 'heading', text: 'Queue'},
        {kind: 'paragraph', text: 'Maximize throughput.'},
        {kind: 'list', ordered: false, items: ['pass the check', 'stay lock-free']},
      ],
      metric: 'total_ops_per_sec · in ops/s · higher is better',
      check: 'vibesys-queue check',
      evaluator: 'vibesys-evaluator-queue 0.2.0',
    });
    expect(view.chart).toBeNull();
    expect(view.current).toEqual([]);
    expect(view.numbers.map(number => number.value)).toEqual(['–', '–', '0', '–']);
    expect(overviewView({trace: EMPTY_TRACE, overview: null}, page()).objective).toBeNull();
  });
});

describe('the objective text', () => {
  const WORDS = ['queue', 'lock-free', 'ring', 'throughput', 'MPMC', 'check'] as const;

  test('keeps headings, paragraphs and lists, in order, from generated Markdown', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const sentence = () =>
        Array.from({length: 1 + random.int(5)}, () => random.pick(WORDS)).join(' ');
      const blocks = Array.from({length: random.int(6)}, () => {
        switch (random.int(3)) {
          case 0:
            return {kind: 'heading' as const, text: sentence()};
          case 1:
            return {kind: 'paragraph' as const, text: sentence()};
          default:
            return {
              kind: 'list' as const,
              ordered: random.bool(),
              items: Array.from({length: 1 + random.int(3)}, sentence),
            };
        }
      });
      const markdown = blocks
        .map(block => {
          if (block.kind === 'heading') return `${'#'.repeat(1 + random.int(3))} ${block.text}`;
          if (block.kind === 'paragraph') return block.text;
          return block.items
            .map((item, n) => (block.ordered ? `${n + 1}. ${item}` : `- ${item}`))
            .join('\n');
        })
        .join('\n\n');
      expect(objectiveBlocks(markdown)).toEqual(blocks);
    }
  });

  test('reduces inline markup to its text and shows other blocks as plain text', () => {
    expect(
      objectiveBlocks(
        'Use **bold**, `code`, _em_ and [a link](https://example.com).\n\n```\nraw block\n```\n> quoted',
      ),
    ).toEqual([
      {kind: 'paragraph', text: 'Use bold, code, em and a link.'},
      {kind: 'paragraph', text: 'raw block'},
      {kind: 'paragraph', text: 'quoted'},
    ]);
  });
});
