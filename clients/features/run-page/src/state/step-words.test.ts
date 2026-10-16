import {describe, expect, test} from 'bun:test';
import type {Span} from '@vibesys/contracts';
import {EMPTY_TRACE, foldTrace} from '@vibesys/run-state/trace';
import {
  mapDetail,
  mapRows,
  overviewView,
  type RunPageState,
  runPageInitial,
  workView,
} from './index.js';
import {stepName} from './span-reading.js';

/**
 * How the plumbing around a search's labelled steps reads: operations and flows the search did not
 * label are named by the runtime's registry, and the words are the client's. A step that waits says
 * what it waits on, and only the stop that was asked for.
 */

const STRATEGIES = ['multi', 'single', 'dynamic', 'evolve', 'plain'] as const;
const OPERATIONS = [
  'render_role_artifacts',
  'interpret_evidence',
  'verify_parent_revision',
  'capture_command',
  'validate_local',
  'export_patch',
  'write_handoff',
] as const;
const SHARED = ['roadmap.read', 'roadmap.put_items', 'agent.turn', 'play_round'] as const;

function span(id: string, parent: string | null, extra: Partial<Span> = {}): Span {
  return {
    id,
    parent,
    type: parent === null || id.startsWith('flow') ? 'flow' : 'operation',
    name: id,
    status: 'running',
    waiting_on: [],
    attempts: [],
    started_at: 0,
    ended_at: null,
    failure: null,
    stop: null,
    ...extra,
  } as Span;
}

function trace(spans: readonly Span[]) {
  return foldTrace(
    EMPTY_TRACE,
    spans.map((each, index) => ({
      sequence: index + 1,
      type: 'span_started',
      data: {kind: 'span_started', span: each},
    })) as never,
  );
}

const ALL = {filter: 'all', open: new Set<string>(), selected: null} as const;

function page(): RunPageState {
  return runPageInitial({
    identity: {host: 'ssh:chelan4', runId: 'run'},
    title: {project: 'queue-rs', task: 'mpmc'},
    hostLabel: 'chelan4',
    unavailable: {},
  });
}

describe('the runtime steps no search labels', () => {
  test('read in words on the Map, never as a registered name', () => {
    const names = [
      ...STRATEGIES.flatMap(strategy => OPERATIONS.map(operation => `${strategy}.${operation}`)),
      ...SHARED,
    ];
    const spans = [
      span('flow:root#1', null, {name: 'multi'}),
      ...names.map((name, index) => span(`op:${index}`, 'flow:root#1', {name})),
    ];
    const rows = mapRows(trace(spans), ALL);
    const titles = rows.slice(1).map(row => row.title);
    expect(titles).toHaveLength(names.length);
    for (const title of titles) expect(title).not.toMatch(/[._]/);
    expect(titles[0]).toBe('Writing the agent’s instructions');
    expect(titles).toContain('Reading the roadmap');
    expect(titles).toContain('Play round');
    // The detail's heading is the same words.
    const detail = mapDetail(trace(spans), {...ALL, selected: 'op:0'});
    expect(detail?.title).toBe('Writing the agent’s instructions');
  });

  test('read in words on the Work tab and in Current work', () => {
    const spans = [
      span('flow:root#1', null, {name: 'multi'}),
      span('flow:round#1', 'flow:root#1', {name: 'play_round'}),
      span('op:render', 'flow:round#1', {name: 'multi.render_role_artifacts'}),
    ];
    const run = {trace: trace(spans), overview: null};
    expect(workView(run, {query: '', outcome: 'all'}).rows.map(row => row.id)).toEqual([
      'Play round',
    ]);
    const current = overviewView(run, page()).current.map(item => [item.title, item.doing]);
    expect(current).toEqual([['Play round', 'Writing the agent’s instructions, 0s']]);
  });

  test('a name the registry does not list reads as it is', () => {
    expect(stepName('measure_the_input')).toBeNull();
    expect(stepName('multi.something_new')).toBeNull();
  });
});

describe('a step that waits', () => {
  const root = span('flow:root#1', null, {
    name: 'multi',
    waiting_on: [
      {kind: 'slot', ref: 'group', since: 5},
      {kind: 'stop', ref: 'stop', since: 5},
    ],
  });

  test('names what it waits on, not the stop nobody asked for', () => {
    const rows = mapRows(trace([root]), ALL);
    expect(rows[0]?.subtitle).toBe('Waiting on its steps to finish');
    const detail = mapDetail(trace([root]), ALL);
    expect(detail?.facts).toContainEqual({
      key: 'Waiting on',
      value: 'Its steps to finish, since 5s into the run',
    });
  });

  test('names the stop once it was asked for', () => {
    const asked = {...root, stop: {requested_at: 9, landed: false}} as Span;
    const rows = mapRows(trace([asked]), ALL);
    expect(rows[0]?.subtitle).toBe('Waiting on its steps to finish, the stop to land');
  });

  test('a task it waits on reads as its title', () => {
    const waiting = span('flow:root#1', null, {
      name: 'multi',
      waiting_on: [{kind: 'slot', ref: 'flow:round#1', since: 0}],
    });
    const round = span('flow:round#1', 'flow:root#1', {name: 'play_round'});
    expect(mapRows(trace([waiting, round]), ALL)[0]?.subtitle).toBe(
      'Waiting on Play round to finish',
    );
  });
});

describe('an adoption step', () => {
  const adoption = (attrs: object, verdict: string) =>
    span('op:adopt', 'flow:root#1', {
      name: 'adopt',
      label: {
        kind: 'adoption',
        title: 'abc1234def5678',
        attrs_json: JSON.stringify(attrs),
        verdict,
        links: [],
      },
    } as Partial<Span>);

  test('reads as adopting the candidate, by a short revision, or the input', () => {
    const spans = [
      span('flow:root#1', null, {name: 'multi'}),
      adoption({candidate: 'abc1234def5678'}, 'adopted'),
    ];
    const row = mapRows(trace(spans), ALL)[1];
    expect(row?.title).toBe('Adopt candidate abc1234');
    expect(mapDetail(trace(spans), {...ALL, selected: 'op:adopt'})?.state.label).toBe('Adopted');
    const input = [span('flow:root#1', null, {name: 'multi'}), adoption({}, 'not_adopted')];
    const other = mapRows(trace(input), ALL)[1];
    expect(other?.title).toBe('Adopt the input');
    expect(mapDetail(trace(input), {...ALL, selected: 'op:adopt'})?.state.label).toBe(
      'Not adopted',
    );
    expect(mapDetail(trace(spans), {...ALL, selected: 'op:adopt'})?.kind).toBe('Adoption');
  });
});

describe('an evaluator command', () => {
  const overview = (check: readonly string[]) => ({
    objective: {text: 'Go faster.', correctness_check: [...check]},
  });

  test('says what its placeholders stand for, never the unexpanded variable', () => {
    const check = [
      'go',
      '-C',
      '/home/shli/evaluators/queue',
      'run',
      '.',
      '--workspace',
      '${PROJECT_ROOT}',
      '${PYTHON}',
      '--also=${PROJECT_ROOT}/src',
    ];
    const view = overviewView({trace: EMPTY_TRACE, overview: overview(check)}, page());
    expect(view.objective?.check).toBe(
      'go -C /home/shli/evaluators/queue run . --workspace <the candidate’s project> python --also=<the candidate’s project>/src',
    );
    expect(view.objective?.check).not.toContain('${');
  });

  test('is shown as it is when it has none', () => {
    const view = overviewView(
      {trace: EMPTY_TRACE, overview: overview(['cargo', 'test', '--release'])},
      page(),
    );
    expect(view.objective?.check).toBe('cargo test --release');
  });
});

describe('a gate row', () => {
  test('writes its numbers as the Overview does', () => {
    const gate = span('op:gate', 'flow:root#1', {
      name: 'measure',
      label: {
        kind: 'gate',
        title: 'benchmark',
        attrs_json: JSON.stringify({
          stage: 'benchmark',
          metric_value: 13318502.550117,
          vs_baseline: 5.7234567,
        }),
        links: [],
      },
    } as Partial<Span>);
    const spans = [span('flow:root#1', null, {name: 'multi'}), gate];
    expect(mapRows(trace(spans), ALL)[1]?.subtitle).toBe('13,318,503 · 5.72× the input');
    const facts = mapDetail(trace(spans), {...ALL, selected: 'op:gate'})?.facts ?? [];
    expect(facts).toContainEqual({key: 'Metric value', value: '13,318,503', mono: true});
    expect(facts).toContainEqual({key: 'Vs baseline', value: '5.72×', mono: true});
  });
});
