import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  AgentRole,
  AgentSchema,
  AgentStatus,
  Harness,
  PendingLeadSchema,
  RunSchema,
  RunStatus,
  UsageSchema,
} from '@vibesys/orchestrator-proto';
import fc from 'fast-check';
import {runSettingsOf} from './run-settings.js';
import {emptyState, type OrchestratorState} from './state.js';

const START = 1_000_000_000;

const agent = (id: string, role: AgentRole, status: AgentStatus) =>
  create(AgentSchema, {
    id,
    runId: 'run-1',
    role,
    harness: Harness.CLAUDE_CODE,
    model: 'model-a',
    status,
  });

/** A state holding one run with the given facts, its history read, and `live` subagents live. */
function stateOf(options: {
  readonly cap: number;
  readonly live: number;
  readonly over: number;
  readonly usd: number;
  readonly setupUsd: number | null;
  readonly limitSeconds: number | null;
  readonly setupSeconds: number | null;
  readonly pending: boolean;
}): OrchestratorState {
  const created = START;
  const optimize =
    options.setupSeconds === null ? undefined : created + options.setupSeconds * 1000;
  const start = optimize ?? created;
  const run = create(RunSchema, {
    id: 'run-1',
    repository: 'repo',
    task: 'task',
    status: RunStatus.RUNNING,
    leadAgentId: 'lead',
    fanoutLimit: options.cap,
    createdAt: timestampFromMs(created),
    usage: create(UsageSchema, {costUsd: options.usd}),
    ...(optimize === undefined ? {} : {optimizeStartedAt: timestampFromMs(optimize)}),
    ...(options.setupUsd === null
      ? {}
      : {setupUsage: create(UsageSchema, {costUsd: options.setupUsd})}),
    ...(options.limitSeconds === null
      ? {}
      : {deadline: timestampFromMs(start + options.limitSeconds * 1000)}),
    ...(options.pending
      ? {pendingLead: create(PendingLeadSchema, {harness: Harness.CODEX, model: 'model-b'})}
      : {}),
  });
  const agents = [
    agent('lead', AgentRole.LEAD, AgentStatus.ACTIVE),
    ...Array.from({length: options.live}, (_, i) =>
      agent(`live-${i}`, AgentRole.WORKER, AgentStatus.ACTIVE),
    ),
    ...Array.from({length: options.over}, (_, i) =>
      agent(`over-${i}`, AgentRole.WORKER, AgentStatus.ENDED),
    ),
  ];
  return {
    ...emptyState,
    runs: {'run-1': run},
    agents: Object.fromEntries(agents.map(each => [each.id, each])),
    histories: {
      'run-1': {
        sequence: 1n,
        complete: true,
        turnsLeftOut: 0,
        checkInsLeftOut: 0,
        agentsLeftOut: 0,
      },
    },
  };
}

const facts = fc.record({
  cap: fc.integer({min: 1, max: 8}),
  live: fc.integer({min: 0, max: 12}),
  over: fc.integer({min: 0, max: 5}),
  usd: fc.integer({min: 0, max: 900}),
  setupUsd: fc.option(fc.integer({min: 0, max: 50}), {nil: null}),
  limitSeconds: fc.option(fc.integer({min: 60, max: 200_000}), {nil: null}),
  setupSeconds: fc.option(fc.integer({min: 0, max: 5000}), {nil: null}),
  pending: fc.boolean(),
});

describe('a run’s live settings', () => {
  it('count the live agents above the cap, and only those that are not over', () => {
    fc.assert(
      fc.property(facts, options => {
        const settings = runSettingsOf(stateOf(options), 'run-1', START + 1000);
        expect(settings?.fanout).toEqual({
          cap: options.cap,
          live: options.live,
          above: Math.max(0, options.live - options.cap),
        });
      }),
    );
  });

  it('keep the setup spend and time apart from the limits', () => {
    fc.assert(
      fc.property(facts, options => {
        const settings = runSettingsOf(stateOf(options), 'run-1', START + 10_000_000);
        expect(settings?.cost.setupUsd).toBe(options.setupUsd);
        expect(settings?.cost.spentUsd).toBe(Math.max(0, options.usd - (options.setupUsd ?? 0)));
        expect(settings?.time.setupSeconds).toBe(options.setupSeconds);
        expect(settings?.time.limitSeconds).toBe(options.limitSeconds);
      }),
    );
  });

  it('show a lead change as pending exactly while the run says one waits', () => {
    fc.assert(
      fc.property(facts, options => {
        const lead = runSettingsOf(stateOf(options), 'run-1', START)?.lead;
        expect(lead?.pending !== null).toBe(options.pending);
        expect(lead?.current?.model).toBe('model-a');
      }),
    );
  });

  it('know no live count before the run’s agents have been read', () => {
    const state = stateOf({
      cap: 2,
      live: 3,
      over: 0,
      usd: 1,
      setupUsd: null,
      limitSeconds: null,
      setupSeconds: null,
      pending: false,
    });
    const unread = {...state, histories: {}};
    expect(runSettingsOf(unread, 'run-1', START)?.fanout).toEqual({cap: 2, live: null, above: 0});
  });
});
