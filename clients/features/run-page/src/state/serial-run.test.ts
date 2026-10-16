import {describe, expect, test} from 'bun:test';
import type {RunEvent, RunOverview, Span} from '@vibesys/contracts';
import {EMPTY_TRACE, foldTrace} from '@vibesys/run-state/trace';
import {
  type KeyNumber,
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

/**
 * The adoption the run ends with, as its span events arrive: the operation that adopts the
 * winner is labelled with the adopted candidate, what it measured and its ratio to the input's,
 * and gets its verdict once core has answered (`tests/vibesys/search_faults.py` holds every
 * search to it). The Overview shows what the run adopted, not what it inferred from its gates.
 */

const ENDED: RunPageState = {
  ...LIVE,
  record: {
    kind: 'ended',
    run: {...(LIVE.record as {run: object}).run, phase: 'completed', end: null},
  } as RunPageState['record'],
};

interface Adopting {
  readonly candidate: string | null;
  readonly value: number | null;
  readonly vs: number | null;
  readonly verdict: 'adopted' | 'not_adopted' | null;
}

function adopting(run: Recording, adoption: Adopting): void {
  run.start('op:adopt.1', 'flow:#1', 'propose_winner', {
    kind: 'adoption',
    title: adoption.candidate ?? 'the input',
    attrs_json: JSON.stringify({
      candidate: adoption.candidate,
      metric_value: adoption.value,
      vs_baseline: adoption.vs,
    }),
  });
  if (adoption.verdict === null) return;
  run.end('op:adopt.1', {verdict: adoption.verdict});
}

function bestOf(run: Recording, state: RunPageState): KeyNumber {
  const best = overviewView(at(run.events), state).numbers.find(
    number => number.label === 'Best vs baseline',
  );
  if (best === undefined) throw new Error('the Overview has no best candidate number');
  return best;
}

describe('the best candidate of a run that adopts', () => {
  test('is the adopted one, even when a passed gate measured better', () => {
    const run = recorded();
    adopting(run, {candidate: 'c1', value: 80, vs: 1.6, verdict: 'adopted'});
    // round 1 measured 100 (2.00×) and passed, but the search adopted a candidate that read 80
    expect(bestOf(run, ENDED)).toEqual({
      label: 'Best vs baseline',
      value: '1.60×',
      sub: '80 ops/s · adopted',
    });
    expect(bestOf(run, LIVE)).toEqual(bestOf(run, ENDED));
  });

  test('is the best passed gate while the run is live and has not adopted yet', () => {
    const run = recorded();
    const expected = {label: 'Best vs baseline', value: '2.00×', sub: '100 ops/s · so far'};
    expect(bestOf(run, LIVE)).toEqual(expected);
    adopting(run, {candidate: 'c1', value: 100, vs: 2, verdict: null});
    expect(bestOf(run, LIVE)).toEqual(expected); // core has not answered: not adopted yet
  });

  test('names no candidate once the run ended without adopting one', () => {
    const run = recorded();
    expect(bestOf(run, ENDED)).toEqual({
      label: 'Best vs baseline',
      value: '–',
      sub: 'no candidate adopted',
    });
    adopting(run, {candidate: 'c1', value: 100, vs: 2, verdict: 'not_adopted'});
    expect(bestOf(run, ENDED).sub).toBe('the adoption failed');
    expect(bestOf(run, LIVE).value).toBe('2.00×'); // while it runs, the gates are all there is
  });

  test('says the input stood when that is what was adopted', () => {
    const run = recorded();
    adopting(run, {candidate: null, value: 50, vs: 1, verdict: 'adopted'});
    expect(bestOf(run, ENDED)).toEqual({
      label: 'Best vs baseline',
      value: '–',
      sub: 'no candidate beat the input',
    });
  });

  test('shows the measured value when the input was never measured', () => {
    const run = recorded();
    adopting(run, {candidate: 'c1', value: 100, vs: null, verdict: 'adopted'});
    expect(bestOf(run, ENDED)).toEqual({
      label: 'Best vs baseline',
      value: '100 ops/s',
      sub: 'adopted',
    });
  });
});

/**
 * The order a live multi run emits its spans (`tests/vibesys/serial_search/test_span_labels.py`
 * plays it): the round's gate is labelled as the measurement is issued, the measurement ends, and
 * only once its evidence is read does the gate's label gain the value, the ratio and the verdict
 * (a `span_updated` of a span that already ended); then the round settles accepted and the next
 * round begins designing.
 */
function liveRoundOne(run: Recording): void {
  run.start('flow:#1', null, 'root');
  run.start('op:measure.1', 'flow:#1', 'measure');
  run.end('op:measure.1');
  const id = 'flow:000001/#1';
  run.start(id, 'flow:#1', 'play_round', {
    kind: 'workstream',
    title: 'round 1',
    attrs_json: JSON.stringify({hypothesis_id: null, parent_revision: null, round: 1}),
  });
  run.relabel(id, {
    title: 'title h1',
    attrs_json: JSON.stringify({hypothesis_id: 'h1', parent_revision: 'base', round: 1}),
  });
  run.start('op:000001/measure.1', id, 'measure', gate());
  run.end('op:000001/measure.1');
  run.relabel('op:000001/measure.1', {
    verdict: 'passed',
    attrs_json: JSON.stringify({stage: 'accuracy,benchmark', metric_value: 100, vs_baseline: 2}),
  });
  run.end(id, {verdict: 'accepted'});
}

describe('a live multi run with an accepted round 1', () => {
  test('mid-run the Overview shows the best candidate and a chart point, and Work its gate', () => {
    const run = new Recording();
    liveRoundOne(run);
    run.start('flow:000002/#1', 'flow:#1', 'play_round', {
      kind: 'workstream',
      title: 'round 2',
      attrs_json: JSON.stringify({hypothesis_id: null, parent_revision: null, round: 2}),
    });
    const view = overviewView(at(run.events), LIVE);
    expect(view.numbers[0]).toEqual({
      label: 'Best vs baseline',
      value: '2.00×',
      sub: '100 ops/s · so far',
    });
    expect(view.numbers[1]).toEqual({label: 'Round', value: '2', sub: 'planning'});
    expect(view.chart?.points.map(point => [point.y, point.filled])).toEqual([[100, true]]);
    const [first] = workView(at(run.events), ALL).rows;
    expect(first?.gate).toBe('passed · 2.00×');
  });

  test('once the run finished the Overview says its last round finished, not working', () => {
    const run = new Recording();
    liveRoundOne(run);
    adopting(run, {candidate: 'c1', value: 100, vs: 2, verdict: 'adopted'});
    run.end('flow:#1');
    const view = overviewView(at(run.events), ENDED);
    expect(view.numbers[1]).toEqual({label: 'Round', value: '1', sub: 'finished'});
    expect(view.numbers[0]?.value).toBe('2.00×');
    expect(view.current).toEqual([]);
  });
});
