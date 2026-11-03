import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  AgentRole,
  AgentSchema,
  AgentStatus,
  AgentStopStage,
  Harness,
  ItemSchema,
  ItemStatus,
  JobOutcome,
  MessageSchema,
  SearchStateSchema,
  TurnSchema,
  TurnStatus,
  TurnTrigger,
  UsageSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {emptyState, type OrchestratorState} from './state.js';
import {agentTree, flattenTree, timeline, unbrokenFrom, usageOf} from './views.js';

const agent = (id: string, parent: string, role: AgentRole, startMs: number) =>
  create(AgentSchema, {
    id,
    runId: 'r',
    parentAgentId: parent,
    role,
    harness: Harness.CODEX,
    status: AgentStatus.IDLE,
    stopStage: AgentStopStage.NONE,
    session: 1,
    startedAt: timestampFromMs(startMs),
  });

const roles = [AgentRole.PLANNER, AgentRole.WORKER, AgentRole.JUDGE];

/** A lead with three subagents, each with turns whose usage reports only some fields. */
const stateWith = (
  turns: readonly {agent: number; input?: number | undefined; cost?: number | undefined}[],
): OrchestratorState => {
  const agents = [
    agent('lead', '', AgentRole.LEAD, 0),
    ...roles.map((role, i) => agent(`a${i}`, 'lead', role, 1 + i)),
  ];
  return {
    ...emptyState,
    agents: Object.fromEntries(agents.map(a => [a.id, a])),
    turns: Object.fromEntries(
      turns.map((turn, i) => [
        `t${i}`,
        create(TurnSchema, {
          id: `t${i}`,
          agentId: agents[turn.agent]?.id ?? 'lead',
          ordinal: i + 1,
          trigger: TurnTrigger.BRIEF,
          triggerMessageId: 'm',
          status: TurnStatus.COMPLETED,
          startedAt: timestampFromMs(i),
          usage: create(UsageSchema, {
            ...(turn.input === undefined ? {} : {inputTokens: BigInt(turn.input)}),
            ...(turn.cost === undefined ? {} : {costUsd: turn.cost}),
          }),
        }),
      ]),
    ),
  };
};

describe('usage', () => {
  it('by agent, by role and for the run add up to the same total, and a field nobody reported stays absent', () => {
    fc.assert(
      fc.property(
        fc.array(
          fc.record({
            agent: fc.integer({min: 0, max: 3}),
            input: fc.option(fc.integer({min: 0, max: 1e6}), {nil: undefined}),
            cost: fc.option(fc.integer({min: 0, max: 500}), {nil: undefined}),
          }),
          {maxLength: 25},
        ),
        turns => {
          const report = usageOf(stateWith(turns), 'r');
          const sum = (values: readonly (number | null)[]) =>
            values.every(value => value === null)
              ? null
              : values.reduce<number>((total, value) => total + (value ?? 0), 0);
          expect(report.run.inputTokens).toBe(sum(turns.map(turn => turn.input ?? null)));
          expect(report.run.costUsd).toBe(sum(turns.map(turn => turn.cost ?? null)));
          expect(sum(Object.values(report.byAgent).map(total => total.inputTokens))).toBe(
            report.run.inputTokens,
          );
          expect(sum(Object.values(report.byRole).map(total => total?.inputTokens ?? null))).toBe(
            report.run.inputTokens,
          );
          expect(report.run.turns).toBe(turns.length);
        },
      ),
    );
  });
});

describe('the agent tree', () => {
  it('puts each agent under its parent, the lead first, and lists every agent once', () => {
    const tree = agentTree(stateWith([]), 'r');
    expect(tree).toHaveLength(1);
    expect(flattenTree(tree).map(node => [node.agent.id, node.depth])).toEqual([
      ['lead', 0],
      ['a0', 1],
      ['a1', 1],
      ['a2', 1],
    ]);
  });
});

describe('reading older steps', () => {
  const stateOf = (ordinals: readonly number[]): OrchestratorState => ({
    ...emptyState,
    items: Object.fromEntries(
      ordinals.map(ordinal => [
        `item-${ordinal}`,
        create(ItemSchema, {
          id: `item-${ordinal}`,
          agentId: 'a',
          ordinal,
          status: ItemStatus.FINISHED,
        }),
      ]),
    ),
  });

  it('paging below the unbroken run fills every hole and reaches the first step', () => {
    fc.assert(
      fc.property(
        fc.integer({min: 1, max: 80}),
        fc.array(fc.integer({min: 1, max: 80}), {minLength: 1, maxLength: 30}),
        fc.integer({min: 1, max: 12}),
        (total, held, pageSize) => {
          const have = new Set(held.filter(ordinal => ordinal <= total));
          if (have.size === 0) have.add(total);
          let state = stateOf([...have]);
          // The host answers "items below this ordinal, newest first".
          for (let reads = 0; reads <= total; reads++) {
            const before = unbrokenFrom(state, 'a');
            if (before === null || before <= 1) break;
            const page = Array.from(
              {length: Math.min(pageSize, before - 1)},
              (_, i) => before - 1 - i,
            );
            state = stateOf([...Object.values(state.items).map(item => item.ordinal), ...page]);
          }
          expect(unbrokenFrom(state, 'a')).toBe(1);
        },
      ),
    );
  });
});

it('timeline job timing uses typed facts and is independent of tool text', () => {
  fc.assert(
    fc.property(fc.integer({min: 0, max: 10000}), fc.string(), (toolAt, text) => {
      const state = {
        ...emptyState,
        searches: {
          r: create(SearchStateSchema, {
            runId: 'r',
            revision: 1n,
            jobs: [{id: 'job-1', startedAt: timestampFromMs(50), finishedAt: timestampFromMs(100)}],
          }),
        },
        messages: {
          m: create(MessageSchema, {
            runId: 'r',
            at: timestampFromMs(200),
            body: {case: 'jobFinished', value: {jobId: 'job-1', outcome: JobOutcome.SUCCEEDED}},
          }),
        },
        items: {
          i: create(ItemSchema, {
            at: timestampFromMs(toolAt),
            body: {case: 'tool', value: {output: `job-1 ${text}`, argsJson: 'job-1'}},
          }),
        },
      };
      expect(timeline(state, 'r', 1000).jobs.map(job => [job.startMs, job.endMs])).toEqual([
        [50, 100],
      ]);
      expect(timeline({...state, searches: {}}, 'r', 1000).jobs).toEqual([]);
    }),
    {seed: 9, examples: [[0, 'job-1']]},
  );
});
