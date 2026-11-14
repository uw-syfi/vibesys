import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {
  type GenericRunEntry,
  presentRunDetail,
  type RunDetailPresentation,
} from '../../../state/index.js';
import {
  GENERIC_RUN_DETAIL_FIXTURES,
  genericRunDetailFixture,
} from '../../../testing/run-detail/index.js';
import {type Absent, overviewOf as overview} from './overview.js';

type Agent = RunDetailPresentation['agents']['items'][number];
type Metric = RunDetailPresentation['optimization']['metrics']['items'][number];

const overviewOf = (
  run: GenericRunEntry,
  detail: RunDetailPresentation | null,
  over: boolean,
  absent: Absent = 'reading',
) => overview(run, detail, over, absent);

const entry = (overrides: Partial<GenericRunEntry> = {}): GenericRunEntry => ({
  runId: 'serve-bespoke-01',
  projectRoot: '/projects/serve',
  state: 'running',
  activity: 'live',
  objective: {name: 'throughput', unit: 'items/s', bestValue: 7200},
  costUsd: 4.25,
  needsUser: false,
  ...overrides,
});

const presentationOf = (
  fixture: (typeof GENERIC_RUN_DETAIL_FIXTURES)[number],
): RunDetailPresentation => {
  const delivery = genericRunDetailFixture(fixture);
  if (!('projection' in delivery) || delivery.projection === null)
    throw new Error(`${fixture} has no projection`);
  return presentRunDetail(delivery.projection);
};

const live = presentationOf('live');

/** The representative run with its first agent replaced. */
const withAgent = (change: Partial<Agent>): RunDetailPresentation => ({
  ...live,
  agents: {
    ...live.agents,
    items: live.agents.items.map((agent, at) => (at === 0 ? {...agent, ...change} : agent)),
  },
});

/** The representative run with its first metric replaced. */
const withMetric = (change: Partial<Metric>): RunDetailPresentation => ({
  ...live,
  optimization: {
    ...live.optimization,
    metrics: {
      ...live.optimization.metrics,
      items: live.optimization.metrics.items.map((metric, at) =>
        at === 0 ? {...metric, ...change} : metric,
      ),
    },
  },
});

const CODE_FIELDS: ReadonlySet<string> = new Set(['key', 'model', 'modelHint']);

const textOf = (value: unknown): readonly string[] => {
  if (typeof value === 'string') return [value];
  if (Array.isArray(value)) return value.flatMap(textOf);
  // A row's `key` is its span id, for React, and a model is a name the harness serves, drawn as code.
  if (typeof value === 'object' && value !== null)
    return Object.entries(value).flatMap(([name, part]) =>
      CODE_FIELDS.has(name) ? [] : textOf(part),
    );
  return [];
};

describe('the overview of a generic run', () => {
  it('reads the representative serving run in words, with each fact the run recorded', () => {
    const overview = overviewOf(entry(), live, false);
    expect(overview.figures.map(figure => [figure.label, figure.value])).toEqual([
      ['Best so far', '7.2k'],
      ['Agents', '1 working'],
      ['Workstreams', '1'],
      ['Checks', '1'],
      ['Spend', '$4.25'],
    ]);
    const [objective, resource] = overview.measures.rows;
    expect(objective).toMatchObject({
      name: 'Throughput',
      role: 'Objective',
      latest: '7.2k items/s',
      change: '+50%',
      readings: '2',
    });
    expect(objective?.hint).toContain('Higher is better.');
    expect(resource).toMatchObject({
      name: 'GPU utilization',
      role: 'Resource use',
      latest: '75 percent',
    });
    expect(overview.agents.rows[0]).toMatchObject({
      name: 'Bespoke serving systems critic',
      stage: 'Implementation',
      state: {label: 'Working'},
      model: 'provider-y · model-x',
    });
    expect(overview.workstreams.rows[0]).toMatchObject({
      title: 'Improve serving throughput',
      state: {label: 'Working'},
      round: '2',
      result: '—',
    });
    expect(overview.checks.rows[0]).toMatchObject({
      check: 'Measurement',
      result: 'Failed check',
      failed: true,
      evidence: '93 of 100 trials completed',
      evidenceHint: 'Correctness check: 93 of 100 trials completed',
      source: 'Candidate evaluation',
    });
  });

  it('never presents a framework source such as gpu as something the run owns', () => {
    const rows = overviewOf(entry(), live, false).checks.rows;
    expect(textOf(rows).join(' ')).not.toMatch(/\bgpu\b/i);
  });

  it('shows no identifier a strategy chose as the program spells it', () => {
    for (const fixture of GENERIC_RUN_DETAIL_FIXTURES) {
      const delivery = genericRunDetailFixture(fixture);
      if (!('projection' in delivery) || delivery.projection === null) continue;
      const overview = overviewOf(entry(), presentRunDetail(delivery.projection), false);
      for (const text of textOf([overview.agents, overview.measures, overview.checks]))
        expect(text, `${fixture}: ${text}`).not.toMatch(/[a-z]+[_-][a-z]+/);
    }
  });

  it('says plainly when nothing was recorded, and never as a zero', () => {
    const overview = overviewOf(
      entry({objective: null, costUsd: null}),
      presentationOf('empty'),
      false,
    );
    expect(overview.figures.map(figure => figure.value)).toEqual(['—', '—', '—', '—', '—']);
    expect(overview.figures.every(figure => figure.tone === 'muted')).toBe(true);
    expect(overview.measures.empty).toBe('No measurements yet');
    expect(overviewOf(entry(), presentationOf('empty'), true).agents.empty).toBe('No agents');
  });

  it('keeps every section in its frame and says it is reading before the first answer', () => {
    const overview = overviewOf(entry(), null, false);
    for (const view of [
      overview.measures,
      overview.agents,
      overview.workstreams,
      overview.checks,
    ]) {
      expect(view.rows).toEqual([]);
      expect(view.empty).toBe('Reading');
    }
    expect(overview.figures.slice(1, 4).map(figure => figure.detail)).toEqual([
      'Reading',
      'Reading',
      'Reading',
    ]);
  });

  it('says the run could not be read, not that it is being read, once the host has refused it', () => {
    const unread = overviewOf(entry(), null, false, 'unread');
    expect(unread.agents.empty).toBe('Not read');
    expect(unread.figures.slice(1, 4).map(figure => figure.detail)).toEqual([
      'Not read',
      'Not read',
      'Not read',
    ]);
  });

  it('says what the app left out of a truncated run and what it could not read', () => {
    const truncated = overviewOf(entry(), presentationOf('truncated'), false);
    expect(truncated.measures.note).toContain('Older entries were left out');
    expect(truncated.dropped).toBe(
      'The oldest were left out to stay within the app’s limits: 2 agents, 5 workstreams, 3 checks, 4 measures.',
    );
    expect(truncated.measures.rows[0]?.readings).toBe('2 of 44');
    expect(truncated.measures.rows[0]?.hint).toContain('42 older readings were left out');
    const unknown = overviewOf(entry(), presentationOf('unknown-descriptor'), false);
    expect(unknown.unreadable).toBe('1 record of the run is in a form this app cannot show.');
    expect(unknown.workstreams.note).toContain('does not know');
  });

  it('writes a metric with no reading as a dash and a flat or single reading as no change', () => {
    const none = overviewOf(entry(), withMetric({points: [], latest: null}), false).measures
      .rows[0];
    expect(none).toMatchObject({latest: '—', change: '—', readings: '—'});
    const [first] = live.optimization.metrics.items[0]?.points ?? [];
    if (first === undefined) throw new Error('the representative run has no reading');
    const one = overviewOf(entry(), withMetric({points: [first], latest: first}), false).measures
      .rows[0];
    expect(one?.change).toBe('—');
    expect(one?.latest).toBe('4.8k items/s');
  });

  it('writes a change too large for a percent as a multiple', () => {
    const [first] = live.optimization.metrics.items[0]?.points ?? [];
    if (first === undefined) throw new Error('the representative run has no reading');
    const points = [
      {...first, value: 2},
      {...first, value: 1024},
    ];
    const [, latest] = points;
    const grown = overviewOf(entry(), withMetric({points, latest: latest ?? null}), false);
    expect(grown.measures.rows[0]?.change).toBe('512×');
  });

  it('puts the objectives before resources and references whatever order the run recorded', () => {
    const [objective, resource] = live.optimization.metrics.items;
    if (objective === undefined || resource === undefined) throw new Error('metrics missing');
    const reversed: RunDetailPresentation = {
      ...live,
      optimization: {
        ...live.optimization,
        metrics: {...live.optimization.metrics, items: [resource, objective]},
      },
    };
    expect(overviewOf(entry(), reversed, false).measures.rows.map(row => row.role)).toEqual([
      'Objective',
      'Resource use',
    ]);
  });

  describe('usage', () => {
    it('is unknown, never zero, for an ended agent that reported nothing', () => {
      const {tokens, cost} = overviewOf(
        entry(),
        withAgent({lifecycle: 'done', usage: undefined}),
        false,
      ).agents.rows[0] ?? {tokens: null, cost: null};
      expect(tokens).toEqual({input: 'unknown', cached: 'unknown', output: 'unknown'});
      expect(cost?.text).toBe('unknown');
    });

    it('is a dash for an agent still working that has reported nothing yet', () => {
      const {tokens, cost} = overviewOf(
        entry(),
        withAgent({lifecycle: 'running', usage: undefined}),
        false,
      ).agents.rows[0] ?? {tokens: null, cost: null};
      expect(tokens).toEqual({input: '—', cached: '—', output: '—'});
      expect(cost?.text).toBe('—');
    });

    it('writes the input apart from the output, with the cached share', () => {
      const row = overviewOf(entry(), live, false).agents.rows[0];
      expect(row?.tokens).toEqual({input: '14', cached: '21%', output: '7'});
      expect(row?.cost.text).toBe('$0.25');
    });

    it('is never a zero where nobody reported, over any mix of reported and missing kinds', () => {
      const count = fc.option(fc.bigInt({min: 0n, max: 10n ** 12n}), {nil: undefined});
      fc.assert(
        fc.property(
          fc.record({
            inputTokens: count,
            cacheReadTokens: count,
            cacheCreationTokens: count,
            outputTokens: count,
            reasoningTokens: count,
            costUsd: fc.option(fc.double({min: 0, max: 1e6, noNaN: true}), {nil: undefined}),
            durationMs: count,
          }),
          fc.constantFrom('running', 'waiting', 'pending', 'done', 'failed', 'stopped'),
          (usage, lifecycle) => {
            const row = overviewOf(entry(), withAgent({usage, lifecycle}), false).agents.rows[0];
            if (row === undefined) return false;
            const unreported = (text: string): boolean => text === 'unknown' || text === '—';
            const noInput = [
              usage.inputTokens,
              usage.cacheReadTokens,
              usage.cacheCreationTokens,
            ].every(kind => kind === undefined);
            return (
              (!noInput || unreported(row.tokens.input)) &&
              (usage.outputTokens !== undefined || unreported(row.tokens.output)) &&
              (usage.costUsd !== undefined || unreported(row.cost.text))
            );
          },
        ),
      );
    });
  });
});
