import {describe, expect, test} from 'bun:test';
import type {RunEvent, RunOverview, Span} from '@vibesys/contracts';
import {EMPTY_TRACE, foldTrace} from '@vibesys/run-state/trace';
import {
  overviewView,
  type RunAtView,
  type RunPageState,
  runPageInitial,
  type WorkFilter,
  workView,
} from './index.js';

/**
 * A multi-agent (serial) run, as its span events arrive: the input's measurement, then one round
 * after another, each a workstream that is labelled `round N` as it begins, named for its
 * hypothesis once the designer chose one, and accepted or rejected as its workspace settles. Each
 * round's designer, implementer and judge turns and its gate are its own operations. The spans are
 * those the multi search's fake-core story records (`tests/vibesys/serial_search/
 * test_span_labels.py`), with times. Regression: before the serial searches labelled their spans,
 * this run's Overview said no candidate had passed, 0 workstreams and nothing measured.
 */

const OVERVIEW: RunOverview = {
  overview_version: 1,
  objective: {text: 'Maximize throughput.', metric: 'throughput', unit: 'ops/s', direction: 'max'},
  benchmarks: {},
};

const LIVE: RunPageState = {
  ...runPageInitial({
    identity: {host: 'ssh:chelan4', runId: 'run'},
    title: {project: 'queue-rs', task: 'mpmc'},
    hostLabel: 'chelan4',
    unavailable: {},
  }),
  record: {
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
  },
};

const ALL: WorkFilter = {query: '', outcome: 'all'};

type Label = NonNullable<Span['label']>;

/** Writes span events as the runtime does: each carries the whole span as it now is. */
class Recording {
  readonly events: RunEvent[] = [];
  readonly #spans = new Map<string, Span>();
  #clock = 0;

  start(id: string, parent: string | null, name: string, label?: Label): void {
    const type = id.startsWith('op:') ? 'operation' : 'flow';
    this.#clock += 10;
    const span: Span = {id, parent, type, name, status: 'running', started_at: this.#clock};
    this.#emit('span_started', label === undefined ? span : {...span, label});
  }

  relabel(id: string, label: Partial<Label>): void {
    const span = this.#spans.get(id) as Span;
    this.#emit('span_updated', {...span, label: {...(span.label as Label), ...label}});
  }

  end(id: string, label?: Partial<Label>): void {
    const span = this.#spans.get(id) as Span;
    this.#clock += 10;
    const ended: Span = {...span, status: 'done', ended_at: this.#clock};
    this.#emit(
      'span_ended',
      label === undefined ? ended : {...ended, label: {...(span.label as Label), ...label}},
    );
  }

  #emit(type: string, span: Span): void {
    this.#spans.set(span.id, span);
    this.events.push({
      protocol_version: 1,
      run_id: 'run',
      sequence: this.events.length + 1,
      timestamp: '2026-10-16T12:00:00Z',
      type,
      data: {kind: type, span},
    } as RunEvent);
  }
}

const turn = (role: string): Label => ({
  kind: 'agent_turn',
  title: role,
  attrs_json: JSON.stringify({role, model: null}),
});

const gate = (): Label => ({
  kind: 'gate',
  title: 'accuracy + benchmark',
  attrs_json: JSON.stringify({stage: 'accuracy,benchmark'}),
});

/** One round: designed, implemented, judged, measured and settled; returns its span id. */
function round(
  run: Recording,
  n: number,
  measured: {value: number; vs: number; verdict: string},
  builds?: string,
): string {
  const id = `flow:00000${n}/#1`;
  const hypothesis = `h${n}`;
  run.start(id, 'flow:#1', 'play_round', {
    kind: 'workstream',
    title: `round ${n}`,
    attrs_json: JSON.stringify({hypothesis_id: null, parent_revision: null, round: n}),
  });
  run.start(`op:00000${n}/orchestrator.1`, id, 'orchestrator', turn('orchestrator'));
  run.end(`op:00000${n}/orchestrator.1`);
  run.relabel(id, {
    title: `title ${hypothesis}`,
    attrs_json: JSON.stringify({
      hypothesis_id: hypothesis,
      parent_revision: builds === undefined ? 'base' : `c${n - 1}`,
      round: n,
    }),
    links: builds === undefined ? [] : [{rel: 'builds_on', to: builds}],
  });
  for (const role of ['implementer', 'judge']) {
    run.start(`op:00000${n}/${role}.1`, id, role, turn(role));
    run.end(`op:00000${n}/${role}.1`);
  }
  run.start(`op:00000${n}/measure.1`, id, 'measure', gate());
  run.end(`op:00000${n}/measure.1`, {
    verdict: measured.verdict,
    attrs_json: JSON.stringify({
      stage: 'accuracy,benchmark',
      metric_value: measured.value,
      vs_baseline: measured.vs,
    }),
  });
  run.end(id, {verdict: 'accepted'});
  return id;
}

function recorded(): Recording {
  const run = new Recording();
  run.start('flow:#1', null, 'root');
  run.start('op:measure.1', 'flow:#1', 'measure');
  run.end('op:measure.1');
  const first = round(run, 1, {value: 100, vs: 2, verdict: 'passed'});
  round(run, 2, {value: 10, vs: 0.2, verdict: 'slower'}, first);
  return run;
}

function at(events: readonly RunEvent[], sequence = events.length): RunAtView {
  return {trace: foldTrace(EMPTY_TRACE, events.slice(0, sequence)), overview: OVERVIEW};
}

describe('a multi-agent run', () => {
  test('regression: the Overview shows the best candidate, its round and the workstreams', () => {
    const view = overviewView(at(recorded().events), LIVE);
    const [best, round, workstreams] = view.numbers;
    expect(best).toEqual({label: 'Best vs baseline', value: '2.00×', sub: '100 ops/s · so far'});
    expect(round).toEqual({label: 'Round', value: '2', sub: 'working'});
    expect(workstreams).toEqual({label: 'Workstreams', value: '2', sub: '0 live · 0 waiting'});
    expect(view.chart?.points.map(point => [point.y, point.filled])).toEqual([
      [100, true],
      [10, true],
    ]);
    expect(view.chart?.reference?.y).toBe(50);
  });

  test('the Work tab lists each round with its hypothesis, agents, gate and what it builds on', () => {
    const view = workView(at(recorded().events), ALL);
    expect(view.labelled).toBe(true);
    expect(
      view.rows.map(row => [row.id, row.hypothesis, row.buildsOn, row.turns, row.gate]),
    ).toEqual([
      ['h1', 'title h1', 'input', '3 · orchestrator, implementer, judge', 'passed · 2.00×'],
      ['h2', 'title h2', 'h1', '3 · orchestrator, implementer, judge', 'slower · 0.20×'],
    ]);
    expect(view.rows.map(row => row.outcome.kind)).toEqual(['accepted', 'accepted']);
  });

  test('a round choosing its hypothesis reads as planning that round', () => {
    const {events} = recorded();
    const designing = events.findIndex(
      event =>
        event.type === 'span_started' &&
        (event.data as {span: Span}).span.id === 'op:000002/orchestrator.1',
    );
    const view = overviewView(at(events, designing + 1), LIVE);
    expect(view.numbers[1]).toEqual({label: 'Round', value: '2', sub: 'planning'});
    expect(view.current.map(item => [item.title, item.doing])).toEqual([
      ['round 2', 'Orchestrator turn, 0s'],
    ]);
  });
});
