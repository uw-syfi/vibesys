import {describe, expect, test} from 'bun:test';
import {type AgentSpend, hostFromKey, hostLabel, type UsageWindowEntry} from '@vibesys/hosts';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  dollars,
  type ModelName,
  NO_BREAKDOWN,
  type UsageLoaded,
  type UsageSource,
  usageView,
} from './usage-view.js';

const ROLES = ['planner', 'optimizer', 'evaluator', null];
const HARNESSES = ['claude', 'codex', null];
const MODELS = ['claude-opus-5-5', 'gpt-6.1-luna', null];
const NAMES: readonly ModelName[] = [
  {harness: 'claude', model: 'claude-opus-5-5', harnessName: 'Claude Code', modelName: 'Opus 5.5'},
  {harness: 'codex', model: 'gpt-6.1-luna', harnessName: 'Codex', modelName: null},
];

/** Cents, so sums of generated costs are exact in floating point. */
function cents(random: Seeded, most: number): number {
  return random.int(most) / 100;
}

/** An answer the helper could give: its rows add up to what it says was spent. */
function entry(random: Seeded, project: number): UsageWindowEntry {
  const byRun = Array.from({length: random.int(6)}, (_, at) => ({
    runId: `run-${project}-${at}`,
    task: random.bool() ? random.pick(['spsc', 'mpmc', 'kv-store']) : null,
    costUsd: cents(random, 5000),
    unpricedTurns: 0,
  }));
  const spent = byRun.reduce((total, run) => total + run.costUsd, 0);
  const left = Math.round(spent * 100);
  const byAgent: AgentSpend[] = [];
  let given = 0;
  const count = left === 0 ? 0 : 1 + random.int(4);
  for (let at = 0; at < count; at += 1) {
    const part = at === count - 1 ? left - given : random.int(left - given + 1);
    given += part;
    byAgent.push({
      role: ROLES[at % ROLES.length] ?? null,
      harness: random.pick(HARNESSES),
      model: random.pick(MODELS),
      costUsd: part / 100,
      turns: 1 + random.int(9),
      unpricedTurns: 0,
    });
  }
  const bests = random.int(4);
  // Unpriced turns belong to a run and to an agent, which the answer says of both.
  const first = byRun[0];
  const firstAgent = byAgent[0];
  const unpriced =
    first === undefined || firstAgent === undefined ? 0 : Math.min(random.int(3), firstAgent.turns);
  if (first !== undefined && firstAgent !== undefined) {
    byRun[0] = {...first, unpricedTurns: unpriced};
    byAgent[0] = {...firstAgent, unpricedTurns: unpriced};
  }
  return {
    since: 0,
    until: 1,
    spentUsd: left / 100,
    spendPerHourUsd: cents(random, 300),
    tokens: {
      input: random.bool() ? random.int(5_000_000) : null,
      cache_read: null,
      cache_write: null,
      output: random.bool() ? random.int(500_000) : null,
      reasoning: null,
    },
    turns: byAgent.reduce((total, agent) => total + agent.turns, 0) + unpriced,
    unpricedTurns: unpriced,
    unpricedItemised: true,
    untimedTurns: random.int(3),
    newBests: bests,
    costPerNewBestUsd: bests === 0 ? null : left / 100 / bests,
    byRun,
    byAgent,
    agentsOmitted: random.int(3),
    missingRuns: Array.from({length: random.int(3)}, (_, at) => `gone-${at}`),
  };
}

function loadedBy(seed: number): UsageLoaded {
  const random = new Seeded(seed);
  const sources: UsageSource[] = Array.from({length: random.int(5)}, (_, at) => ({
    host: random.pick(['local', 'ssh:host-a', 'ssh:host-b'] as const),
    project: `/work/project-${at}`,
    usage: entry(random, at),
    leftOut: random.int(3),
  }));
  return {
    sources,
    names: NAMES,
    unreachable: random.int(3),
    unsupported: random.int(3),
  };
}

const sumCents = (values: readonly number[]): number =>
  Math.round(values.reduce((total, value) => total + value * 100, 0));

function expectCostliestFirst(
  rows: readonly {readonly costUsd: number; readonly share: number}[],
): void {
  for (const [at, row] of rows.entries()) {
    expect(row.share).toBeGreaterThanOrEqual(0);
    expect(row.share).toBeLessThanOrEqual(1);
    const before = rows[at - 1];
    if (before === undefined) continue;
    expect(before.costUsd).toBeGreaterThanOrEqual(row.costUsd);
    expect(before.share).toBeGreaterThanOrEqual(row.share);
  }
}

describe('usageView', () => {
  test('property: the rows add up to what the tile says was spent', () => {
    for (const seed of SEEDS) {
      const loaded = loadedBy(seed);
      const view = usageView(loaded);
      const spent = sumCents(loaded.sources.map(source => source.usage.spentUsd));
      expect(view.tiles[0]?.figure).toBe(dollars(spent / 100));
      expect(sumCents(view.runs.map(run => run.costUsd))).toBe(spent);
      const omitted = view.agentsOmitted;
      const agents = sumCents(view.agents.map(agent => agent.costUsd));
      // The helper's agent rows are complete unless it omitted some.
      expect(agents).toBeLessThanOrEqual(spent);
      if (omitted === 0) expect(agents).toBe(spent);
      expect(view.agents.reduce((total, agent) => total + agent.turns, 0)).toBe(
        loaded.sources.reduce(
          (total, source) =>
            total + source.usage.byAgent.reduce((turns, agent) => turns + agent.turns, 0),
          0,
        ),
      );
    }
  });

  test('property: rows are costliest first, shares fall from one, and keys are distinct', () => {
    for (const seed of SEEDS) {
      const view = usageView(loadedBy(seed));
      for (const rows of [view.runs, view.agents]) {
        expectCostliestFirst(rows);
        const keys = rows.map(row => row.key);
        expect(new Set(keys).size).toBe(keys.length);
      }
      expect(view.runs[0]?.share ?? 1).toBe(view.runs[0]?.costUsd === 0 ? 0 : 1);
    }
  });

  test('property: what the answers could not place is shown, never dropped', () => {
    for (const seed of SEEDS) {
      const loaded = loadedBy(seed);
      const view = usageView(loaded);
      const answers = loaded.sources.map(source => source.usage);
      const label = (key: string): string | undefined =>
        view.status.find(each => each.key === key)?.label;
      const count = (key: string): number => Number.parseInt(label(key) ?? '0', 10);
      expect(count('unpriced')).toBe(answers.reduce((total, a) => total + a.unpricedTurns, 0));
      expect(count('untimed')).toBe(answers.reduce((total, a) => total + a.untimedTurns, 0));
      expect(count('not-counted')).toBe(
        answers.reduce((total, a) => total + a.missingRuns.length, 0) +
          loaded.sources.reduce((total, source) => total + source.leftOut, 0),
      );
      expect(count('unreachable')).toBe(loaded.unreachable);
      expect(count('unsupported')).toBe(loaded.unsupported);
      expect(view.agentsOmitted).toBe(answers.reduce((total, a) => total + a.agentsOmitted, 0));
      // A zero count is no status.
      expect(view.status.every(each => !each.label.startsWith('0 '))).toBe(true);
    }
  });

  test('property: each unpriced count is explained by the runs and agents that hold its turns', () => {
    for (const seed of SEEDS) {
      const loaded = loadedBy(seed);
      const view = usageView(loaded);
      const status = view.status.find(each => each.key === 'unpriced');
      const total = loaded.sources.reduce((sum, source) => sum + source.usage.unpricedTurns, 0);
      expect(status?.runs.reduce((sum, run) => sum + run.count, 0) ?? 0).toBe(total);
      expect(status?.parts.length ?? 0).toBe(view.agents.filter(row => row.unpriced > 0).length);
      // The rows say it too: a row's unpriced count is its own, and none is more than its turns.
      expect(view.runs.reduce((sum, row) => sum + row.unpriced, 0)).toBe(total);
      expect(view.agents.reduce((sum, row) => sum + row.unpriced, 0)).toBe(total);
      for (const row of view.agents) expect(row.unpriced).toBeLessThanOrEqual(row.turns);
    }
  });

  test('property: an unpriced count the helper cannot itemise says so, naming the machine', () => {
    for (const seed of SEEDS) {
      const loaded = loadedBy(seed);
      const old = loaded.sources.map((source, at) => ({
        ...source,
        usage: {...source.usage, unpricedItemised: at % 2 === 0},
      }));
      const view = usageView({...loaded, sources: old});
      const status = view.status.find(each => each.key === 'unpriced');
      const unexplained = old.filter(
        source => source.usage.unpricedTurns > 0 && !source.usage.unpricedItemised,
      );
      // One line per such machine, and a count nothing lacks is explained by its rows alone.
      expect(status?.notes ?? []).toEqual(
        unexplained.map(source => `${hostLabel(hostFromKey(source.host))}: ${NO_BREAKDOWN}`),
      );
      for (const other of view.status.filter(each => each.key !== 'unpriced')) {
        expect(other.notes).toEqual([]);
      }
    }
  });

  test('property: runs not counted are named, by project, and the ones past the bound by count', () => {
    for (const seed of SEEDS) {
      const loaded = loadedBy(seed);
      const status = usageView(loaded).status.find(each => each.key === 'not-counted');
      const missing = loaded.sources.flatMap(source => source.usage.missingRuns);
      expect(status?.runs.map(run => run.runId) ?? []).toEqual(missing);
      const past = loaded.sources.filter(source => source.leftOut > 0);
      expect(status?.parts ?? []).toEqual(
        past.map(source => `${source.project.split('/').at(-1)}: ${source.leftOut}`),
      );
    }
  });

  test('a run is named by its task, else its id; a model by the catalogue, else its id', () => {
    const base = entry(new Seeded(3), 0);
    const loaded: UsageLoaded = {
      sources: [
        {
          host: 'local',
          project: '/p',
          leftOut: 0,
          usage: {
            ...base,
            spentUsd: 3,
            byRun: [
              {runId: 'run-a', task: 'spsc', costUsd: 2, unpricedTurns: 0},
              {runId: 'run-b', task: null, costUsd: 1, unpricedTurns: 0},
            ],
            byAgent: [
              {
                role: 'planner',
                harness: 'claude',
                model: 'claude-opus-5-5',
                costUsd: 2,
                turns: 4,
                unpricedTurns: 0,
              },
              {
                role: 'optimizer',
                harness: 'codex',
                model: 'gpt-6.1-luna',
                costUsd: 1,
                turns: 2,
                unpricedTurns: 0,
              },
              {role: null, harness: null, model: null, costUsd: 0, turns: 1, unpricedTurns: 1},
            ],
          },
        },
      ],
      names: NAMES,
      unreachable: 0,
      unsupported: 0,
    };
    const view = usageView(loaded);
    expect(view.runs.map(run => run.name)).toEqual(['spsc', 'run-b']);
    expect(view.agents.map(agent => [agent.role, agent.harness, agent.model])).toEqual([
      ['Planner', 'Claude Code', {name: 'Opus 5.5', id: 'claude-opus-5-5'}],
      ['Optimizer', 'Codex', {name: 'gpt-6.1-luna', id: null}],
      ['unknown', 'unknown', {name: 'unknown', id: null}],
    ]);
  });

  test('the same agent in two projects is one row with the sum', () => {
    const agent: AgentSpend = {
      role: 'planner',
      harness: 'claude',
      model: 'claude-opus-5-5',
      costUsd: 1.5,
      turns: 3,
      unpricedTurns: 1,
    };
    const base = entry(new Seeded(9), 0);
    const source = (project: string): UsageSource => ({
      host: 'local',
      project,
      usage: {...base, spentUsd: 1.5, byRun: [], byAgent: [agent], agentsOmitted: 0},
      leftOut: 0,
    });
    const view = usageView({
      sources: [source('/a'), source('/b')],
      names: NAMES,
      unreachable: 0,
      unsupported: 0,
    });
    expect(view.agents).toHaveLength(1);
    expect(view.agents[0]).toMatchObject({costUsd: 3, turns: 6, cost: '$3.00', unpriced: 2});
  });

  test('a window with no turn is empty; no new best reads none, never a division by zero', () => {
    const view = usageView({
      sources: [],
      names: [],
      unreachable: 0,
      unsupported: 0,
    });
    expect(view.empty).toBe(true);
    expect(view.tiles.map(tile => tile.figure)).toEqual(['$0.00', '$0.00/h', 'unknown', 'none']);
    expect(view.runs).toEqual([]);
  });

  test('the tiles say tokens as the Usage face does, and the cost of a best', () => {
    const base = entry(new Seeded(5), 0);
    const view = usageView({
      sources: [
        {
          host: 'local',
          project: '/p',
          leftOut: 0,
          usage: {
            ...base,
            spentUsd: 12,
            spendPerHourUsd: 1.25,
            unpricedTurns: 0,
            turns: 7,
            newBests: 3,
            tokens: {
              input: 1_200_000,
              cache_read: null,
              cache_write: null,
              output: 48_000,
              reasoning: null,
            },
          },
        },
      ],
      names: [],
      unreachable: 0,
      unsupported: 0,
    });
    expect(view.tiles).toEqual([
      {name: 'Spent', figure: '$12.00', detail: '7 turns'},
      {name: 'Per hour now', figure: '$1.25/h', detail: null},
      {name: 'Tokens', figure: '1.2M', detail: 'in 1.2M · out 48k'},
      {name: 'Per new best', figure: '$4.00', detail: '3 new bests'},
    ]);
  });

  test('no sentence and no em dash in any word the view writes', () => {
    for (const seed of SEEDS.slice(0, 60)) {
      const view = usageView(loadedBy(seed));
      const words = [
        ...view.tiles.flatMap(tile => [tile.name, tile.figure, tile.detail ?? '']),
        ...view.status.map(each => each.label),
      ];
      for (const text of words) {
        expect(text).not.toContain('—');
        expect(text).not.toMatch(/[.!?]( |$)/);
      }
    }
  });
});

describe('the order of runs that cost the same', () => {
  test('is total: equal costs fall in name order, then run order, whatever order they arrive in', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const base = entry(random, 0);
      const byRun = Array.from({length: 2 + random.int(8)}, (_, at) => ({
        runId: `run-${at}`,
        task: random.pick(['spsc', 'mpmc', null]),
        costUsd: random.pick([1, 2]),
        unpricedTurns: 0,
      }));
      const view = (rows: typeof byRun) =>
        usageView({
          sources: [{host: 'local', project: '/p', usage: {...base, byRun: rows}, leftOut: 0}],
          names: NAMES,
          unreachable: 0,
          unsupported: 0,
        }).runs.map(row => row.key);

      const shuffled = [...byRun].sort(() => random.next() - 0.5);

      expect(view(shuffled)).toEqual(view(byRun));
    }
  });
});
