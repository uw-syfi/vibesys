import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  type Agent,
  AgentRole,
  AgentSchema,
  AgentStatus,
  AgentStopStage,
  Delivery,
  HandoffSchema,
  Harness,
  type Message,
  MessageSchema,
  RunStatus,
  SubagentSettledSchema,
  UsageSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {runOf} from '../../testing/orchestrator/builders.js';
import {emptyState, type OrchestratorState} from '../orchestrator/state.js';
import {
  type AgentRow,
  agentKey,
  type FoldChoices,
  foldedCount,
  GROUP_FROM,
  type GroupRow,
  groupKey,
  isFolded,
  type SpawnRow,
  type SpawnTree,
  spawnTree,
  toggled,
} from './spawn-tree.js';

const ROLES = [
  AgentRole.PLANNER,
  AgentRole.WORKER,
  AgentRole.JUDGE,
  AgentRole.COMBINER,
  AgentRole.PROFILER,
] as const;
const STATUSES = [
  AgentStatus.LAUNCHING,
  AgentStatus.ACTIVE,
  AgentStatus.WAITING,
  AgentStatus.IDLE,
  AgentStatus.ENDED,
  AgentStatus.FAILED,
  AgentStatus.LOST,
] as const;
const RUN = 'run';
const NOW = 10_000_000;

interface Spec {
  /** Index of the parent among the agents; -1 for none. May point forward or at itself. */
  readonly parent: number;
  readonly role: number;
  readonly status: number;
  readonly start: number;
  readonly length: number;
  readonly handsBack: boolean;
  readonly settles: boolean;
  readonly cost: number | null;
  readonly harness: number;
  readonly model: string;
}

const spec = (count: number): fc.Arbitrary<Spec> =>
  fc.record({
    parent: fc.integer({min: -1, max: Math.max(-1, count - 1)}),
    role: fc.integer({min: 0, max: ROLES.length - 1}),
    status: fc.integer({min: 0, max: STATUSES.length - 1}),
    start: fc.integer({min: 0, max: 5_000}),
    length: fc.integer({min: 0, max: 5_000}),
    handsBack: fc.boolean(),
    settles: fc.boolean(),
    cost: fc.option(fc.integer({min: 0, max: 900}), {nil: null}),
    harness: fc.integer({min: 0, max: 1}),
    model: fc.string({maxLength: 32}),
  });

/** A tree whose parents always come earlier: no cycles. */
const treeSpecs = fc.integer({min: 0, max: 40}).chain(count =>
  fc.array(spec(count), {minLength: count, maxLength: count}).map(specs =>
    specs.map((one, index) => ({
      ...one,
      parent: index === 0 ? -1 : Math.min(one.parent, index - 1),
    })),
  ),
);

/** Any parent links at all: forward, self and cyclic ones included. */
const wildSpecs = fc
  .integer({min: 0, max: 30})
  .chain(count => fc.array(spec(count), {minLength: count, maxLength: count}));

const idOf = (index: number): string => `a${index}`;

function agentOf(one: Spec, index: number, specs: readonly Spec[]): Agent {
  const role =
    index === 0 && one.parent === -1 ? AgentRole.LEAD : (ROLES[one.role] ?? AgentRole.WORKER);
  const status = STATUSES[one.status] ?? AgentStatus.ACTIVE;
  const over =
    status === AgentStatus.ENDED || status === AgentStatus.FAILED || status === AgentStatus.LOST;
  return create(AgentSchema, {
    id: idOf(index),
    runId: RUN,
    parentAgentId: one.parent === -1 ? '' : idOf(one.parent),
    role,
    harness: one.harness === 0 ? Harness.CLAUDE_CODE : Harness.CODEX,
    model: one.model,
    status,
    stopStage: AgentStopStage.NONE,
    session: 1,
    startedAt: timestampFromMs(one.start),
    ...(over ? {endedAt: timestampFromMs(one.start + one.length)} : {}),
    ...(one.cost === null ? {} : {usage: create(UsageSchema, {costUsd: one.cost / 100})}),
    // Not read; keeps `specs` in the signature for the generators that need the whole list.
    reasoningEffort: specs.length > 0 ? '' : '',
  });
}

function messagesOf(specs: readonly Spec[]): Message[] {
  return specs.flatMap((one, index): Message[] => {
    if (one.parent === -1) return [];
    const at = timestampFromMs(one.start + Math.floor(one.length / 2));
    const base = {runId: RUN, delivery: Delivery.CONSUMED, at};
    return [
      ...(one.handsBack
        ? [
            create(MessageSchema, {
              ...base,
              id: `h${index}`,
              fromAgentId: idOf(index),
              toAgentId: idOf(one.parent),
              body: {case: 'handoff', value: create(HandoffSchema, {claim: 'did it'})},
            }),
          ]
        : []),
      ...(one.settles
        ? [
            create(MessageSchema, {
              ...base,
              id: `s${index}`,
              toAgentId: idOf(one.parent),
              body: {
                case: 'subagentSettled',
                value: create(SubagentSettledSchema, {
                  agentId: idOf(index),
                  status: AgentStatus.IDLE,
                }),
              },
            }),
          ]
        : []),
    ];
  });
}

function stateOf(specs: readonly Spec[], over = false): OrchestratorState {
  const agents = specs.map(agentOf);
  const run = {
    ...runOf({
      id: RUN,
      leadAgentId: 'a0',
      status: over ? RunStatus.ENDED : RunStatus.RUNNING,
      startMs: 0,
      fanout: 5,
    }),
    ...(over ? {endedAt: timestampFromMs(NOW - 1_000)} : {}),
  };
  return {
    ...emptyState,
    runs: {[RUN]: run},
    agents: Object.fromEntries(agents.map(agent => [agent.id, agent])),
    messages: Object.fromEntries(messagesOf(specs).map(message => [message.id, message])),
  };
}

/** Every fold key of a state, each with a choice of folded, open, or none. */
function choicesOf(state: OrchestratorState, picks: readonly number[]): FoldChoices {
  const keys = Object.values(state.agents).flatMap(agent => [
    agentKey(agent.id),
    ...ROLES.map(role => groupKey(agent.id, role)),
  ]);
  return Object.fromEntries(
    keys.flatMap((key, index): [string, boolean][] => {
      const pick = picks[index % Math.max(1, picks.length)] ?? 2;
      return pick === 2 ? [] : [[key, pick === 1]];
    }),
  );
}

const picks = fc.array(fc.integer({min: 0, max: 2}), {maxLength: 12});

/** The agents a tree covers: its agent rows, and what each folded row hides. */
function covered(rows: readonly SpawnRow[]): string[] {
  return rows.flatMap(row =>
    row.kind === 'agent' ? [row.agentId, ...row.hidden] : row.folded ? row.agentIds : [],
  );
}

const agentRows = (rows: readonly SpawnRow[]): AgentRow[] =>
  rows.filter((row): row is AgentRow => row.kind === 'agent');

const tree = (state: OrchestratorState, folds: FoldChoices) =>
  spawnTree(state, RUN, {nowMs: NOW, host: 'host-a', folds});

const below = (state: OrchestratorState, id: string): string[] =>
  Object.values(state.agents)
    .filter(agent => agent.parentAgentId === id)
    .flatMap(child => [child.id, ...below(state, child.id)]);

/** The row sits where its parent's row says: under it, or under the open group that holds it. */
function expectPlaced(rows: readonly SpawnRow[], row: SpawnRow, index: number): void {
  expect(row.lineage).toHaveLength(row.depth);
  const next = rows[index + 1];
  if (isFolded(row) && next !== undefined) expect(next.depth).toBeLessThanOrEqual(row.depth);
  if (row.kind !== 'agent' || row.parentId === null) return;
  const above = rows
    .slice(0, index)
    .reverse()
    .find(candidate => candidate.depth < row.depth);
  const parent =
    above?.kind === 'group'
      ? rows.find(candidate => candidate.kind === 'agent' && candidate.agentId === above.parentId)
      : above;
  expect(parent?.kind === 'agent' ? parent.agentId : undefined).toBe(row.parentId);
  expect(above?.kind === 'group' ? above.folded : false).toBe(false);
  expect(row.depth - (above?.depth ?? -1)).toBe(1);
}

/** A folded row stands for exactly the agents under it. */
function expectCounted(state: OrchestratorState, row: SpawnRow): void {
  if (row.kind === 'agent') {
    expect(foldedCount(row)).toBe(row.hidden.length);
    if (row.hidden.length > 0)
      expect([...row.hidden].sort()).toEqual(below(state, row.agentId).sort());
    return;
  }
  const members = Object.values(state.agents)
    .filter(agent => agent.parentAgentId === row.parentId && agent.role === row.role)
    .map(agent => agent.id);
  expect([...row.agentIds].sort()).toEqual(members.flatMap(id => [id, ...below(state, id)]).sort());
  expect(foldedCount(row)).toBe(row.folded ? row.agentIds.length : 0);
  expect(members.length).toBeGreaterThanOrEqual(GROUP_FROM);
}

/** A row lies on the axis; a group covers its agents; a tick lies on its parent's bar. */
function expectSpanned(state: OrchestratorState, result: SpawnTree, row: SpawnRow): void {
  expect(row.startMs).toBeLessThanOrEqual(row.endMs);
  expect(row.startMs).toBeGreaterThanOrEqual(result.startMs);
  expect(row.endMs).toBeLessThanOrEqual(result.endMs);
  expect(row.host).toBe('host-a');
  if (row.kind === 'agent') {
    const agent = state.agents[row.agentId];
    expect(row.runtime).toEqual({harness: agent?.harness, model: agent?.model});
    for (const tick of row.ticks) {
      expect(tick.atMs).toBeGreaterThanOrEqual(row.startMs);
      expect(tick.atMs).toBeLessThanOrEqual(row.endMs);
      expect(state.agents[tick.childId]?.parentAgentId).toBe(row.agentId);
    }
    return;
  }
  for (const id of row.agentIds) {
    const agent = state.agents[id];
    expect(row.startMs).toBeLessThanOrEqual(Number(agent?.startedAt?.seconds ?? 0) * 1000);
  }
}

describe('the spawn tree', () => {
  it('covers every agent exactly once, whatever the parent links and the folds', () => {
    fc.assert(
      fc.property(wildSpecs, picks, (specs, pick) => {
        const state = stateOf(specs);
        const result = tree(state, choicesOf(state, pick));
        expect([...covered(result.rows)].sort()).toEqual(Object.keys(state.agents).sort());
        expect(new Set(result.rows.map(row => row.key)).size).toBe(result.rows.length);
      }),
    );
  });

  it('puts an agent under its parent, one level deeper, and nothing under a folded row', () => {
    fc.assert(
      fc.property(treeSpecs, picks, (specs, pick) => {
        const state = stateOf(specs);
        const rows = tree(state, choicesOf(state, pick)).rows;
        rows.forEach((row, index) => {
          expectPlaced(rows, row, index);
        });
      }),
    );
  });

  it('counts a folded row as the agents it hides, which are the agents under it', () => {
    fc.assert(
      fc.property(treeSpecs, picks, (specs, pick) => {
        const state = stateOf(specs);
        for (const row of tree(state, choicesOf(state, pick)).rows) expectCounted(state, row);
      }),
    );
  });

  it('opening every row shows every agent as its own row', () => {
    fc.assert(
      fc.property(wildSpecs, specs => {
        const state = stateOf(specs);
        const open = Object.fromEntries(
          Object.values(state.agents).flatMap(agent => [
            [agentKey(agent.id), false] as const,
            ...ROLES.map(role => [groupKey(agent.id, role), false] as const),
          ]),
        );
        const rows = tree(state, open).rows;
        expect(
          agentRows(rows)
            .map(row => row.agentId)
            .sort(),
        ).toEqual(Object.keys(state.agents).sort());
        expect(rows.some(isFolded)).toBe(false);
      }),
    );
  });

  it('toggling a row flips only that row', () => {
    fc.assert(
      fc.property(treeSpecs, picks, fc.nat(), (specs, pick, which) => {
        const state = stateOf(specs);
        const folds = choicesOf(state, pick);
        const rows = tree(state, folds).rows;
        const row = rows[which % Math.max(1, rows.length)];
        if (row === undefined) return;
        const after = tree(state, toggled(folds, row)).rows.find(
          candidate => candidate.key === row.key,
        );
        if (row.kind === 'agent' && row.children === 0) {
          expect(after !== undefined && isFolded(after)).toBe(false);
        } else {
          expect(after !== undefined && isFolded(after)).toBe(!isFolded(row));
        }
      }),
    );
  });

  it('spans each bar from its start to an end no earlier, a group over its agents, and ticks on the parent bar', () => {
    fc.assert(
      fc.property(wildSpecs, fc.boolean(), picks, (specs, over, pick) => {
        const state = stateOf(specs, over);
        const result = tree(state, choicesOf(state, pick));
        for (const row of result.rows) expectSpanned(state, result, row);
        expect(result.startMs).toBeLessThanOrEqual(result.endMs);
      }),
    );
  });

  it('counts agents for the header the way the agents say', () => {
    fc.assert(
      fc.property(wildSpecs, specs => {
        const state = stateOf(specs);
        const {stats} = tree(state, {});
        const agents = Object.values(state.agents);
        expect(stats.total).toBe(agents.length);
        expect(stats.failed).toBe(
          agents.filter(a => a.status === AgentStatus.FAILED || a.status === AgentStatus.LOST)
            .length,
        );
        expect(stats.running).toBe(
          agents.filter(a =>
            [AgentStatus.LAUNCHING, AgentStatus.ACTIVE, AgentStatus.WAITING].includes(a.status),
          ).length,
        );
        expect(stats.peak).toBeLessThanOrEqual(agents.length);
        expect(stats.peak).toBeGreaterThanOrEqual(stats.running);
      }),
    );
  });

  it('no agent is working once the run is over, and nothing outlives the run', () => {
    fc.assert(
      fc.property(wildSpecs, specs => {
        const state = stateOf(specs, true);
        const result = tree(state, {});
        expect(result.stats.running).toBe(0);
        for (const row of result.rows) expect(row.state === 'running').toBe(false);
      }),
    );
  });
});

describe('a spawn tree like the mockup', () => {
  const specs: Spec[] = [
    {
      parent: -1,
      role: 0,
      status: 1,
      start: 0,
      length: 0,
      handsBack: false,
      settles: false,
      cost: 6600,
      harness: 1,
      model: '',
    },
    {
      parent: 0,
      role: 0,
      status: 4,
      start: 300,
      length: 19_700,
      handsBack: true,
      settles: false,
      cost: 1500,
      harness: 0,
      model: 'model-a',
    },
    ...Array.from(
      {length: 12},
      (_, i): Spec => ({
        parent: 1,
        role: 1,
        status: i === 4 ? 5 : 4,
        start: 800 + i * 50,
        length: 5_000 + i * 100,
        handsBack: true,
        settles: false,
        cost: 700,
        harness: i % 2,
        model: `model-${i % 2}`,
      }),
    ),
    {
      parent: 0,
      role: 0,
      status: 1,
      start: 20_000,
      length: 0,
      handsBack: false,
      settles: false,
      cost: 2200,
      harness: 1,
      model: 'model-b',
    },
  ];
  const state = stateOf(specs);

  it('folds the twelve workers of the first planner into one count bar with a tick for each', () => {
    const rows = tree(state, {}).rows;
    const group = rows.find((row): row is GroupRow => row.kind === 'group');
    expect(group?.folded).toBe(true);
    expect(group?.agentIds).toHaveLength(12);
    expect(group?.failed).toBe(1);
    expect(group?.state).toBe('failed');
    expect(group?.runtimes).toEqual([
      {harness: Harness.CLAUDE_CODE, model: 'model-0'},
      {harness: Harness.CODEX, model: 'model-1'},
    ]);
    const planner = rows.find(
      (row): row is AgentRow => row.kind === 'agent' && row.agentId === 'a1',
    );
    expect(planner?.ticks).toHaveLength(12);
    expect(planner?.name).toEqual({role: AgentRole.PLANNER, number: 1});
    expect(agentRows(rows).map(row => row.agentId)).toEqual(['a0', 'a1', 'a14']);
  });

  it('shows the workers as rows under their planner once the group is opened', () => {
    const rows = tree(state, {[groupKey('a1', AgentRole.WORKER)]: false}).rows;
    expect(rows.map(row => row.depth).slice(0, 5)).toEqual([0, 1, 2, 3, 3]);
    expect(agentRows(rows)).toHaveLength(15);
  });

  it('shows the cost the host reported on an agent, and nothing for an agent it said nothing of', () => {
    const rows = tree(state, {}).rows;
    const lead = rows[0];
    expect(lead?.kind === 'agent' ? lead.costUsd : undefined).toBe(66);
    const without = stateOf([{...(specs[0] as Spec), cost: null}]);
    const only = tree(without, {}).rows[0];
    expect(only?.kind === 'agent' ? only.costUsd : undefined).toBeNull();
  });
});
