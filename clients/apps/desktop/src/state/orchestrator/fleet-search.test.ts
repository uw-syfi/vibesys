import {create} from '@bufbuild/protobuf';
import {timestampFromMs} from '@bufbuild/protobuf/wkt';
import {describe, expect, it} from '@effect/vitest';
import {
  AllowedAgentSchema,
  Harness,
  IdeaViewSchema,
  MessageSchema,
  PlanSchema,
  RecordOriginSchema,
  ReportKind,
  ReportSchema,
  ReportScope,
  RunSchema,
  RunSearchResultSchema,
  RunStatus,
  RunSummarySchema,
  SearchStateSchema,
  TaskCardSchema,
  WorkstreamSchema,
  WorkstreamStatus,
} from '@vibesys/orchestrator-proto';
import {
  FLEET_SEARCH_REGISTRY,
  type FleetHostInput,
  fleetSearchScore,
  fleetView,
  NO_FLEET_FILTER,
} from './fleet.js';
import {emptyState} from './state.js';

const host = (): FleetHostInput => {
  const alpha = create(RunSchema, {
    id: 'run-alpha',
    task: 'KV cache sprint',
    repository: '/work/inference-engine',
    status: RunStatus.RUNNING,
    createdAt: timestampFromMs(1_000),
    taskCard: create(TaskCardSchema, {
      task: 'KV cache sprint',
      heading: 'Make serving more predictable',
      modelName: 'Llama 3.1 70B',
      workloadDescription: 'Reduce tail latency under bursty traffic',
      objectives: [{name: 'throughput', unit: 'tokens per second'}],
    }),
    allowedAgents: [
      create(AllowedAgentSchema, {harness: Harness.CODEX, model: 'o3'}),
      create(AllowedAgentSchema, {harness: Harness.CLAUDE_CODE, model: 'sonnet'}),
    ],
  });
  const beta = create(RunSchema, {
    id: 'run-beta',
    task: 'Database compaction',
    repository: '/work/storage',
    status: RunStatus.ENDED,
    createdAt: timestampFromMs(2_000),
    endedAt: timestampFromMs(4_000),
    taskCard: create(TaskCardSchema, {
      task: 'Database compaction',
      heading: 'Tune background compaction',
      workloadDescription: 'Lower write amplification',
    }),
  });
  const report = create(MessageSchema, {
    id: 'report-alpha',
    runId: alpha.id,
    at: timestampFromMs(6_000),
    body: {
      case: 'report',
      value: create(ReportSchema, {
        headline: 'Paged attention prototype is running',
        changed: 'Added a bounded page allocator',
        inFlight: 'Measuring long-context requests',
        decisions: 'Keep the contiguous baseline',
        kind: ReportKind.PROGRESS,
        scope: ReportScope.RUN,
        sequence: 3,
      }),
    },
  });
  const origin = create(RecordOriginSchema, {runId: alpha.id, agent: 'lead'});
  const search = create(SearchStateSchema, {
    runId: alpha.id,
    revision: 9n,
    workstreams: [
      create(WorkstreamSchema, {
        number: 1,
        title: 'Prefix cache layout',
        note: 'Testing a segmented index',
        status: WorkstreamStatus.ACTIVE,
        origin,
        updatedAt: timestampFromMs(5_000),
      }),
    ],
    plans: [
      create(PlanSchema, {
        number: 4,
        title: 'Reuse block metadata',
        body: 'Avoid rebuilding the lookup table for every batch.',
        origin,
        updatedAt: timestampFromMs(7_000),
      }),
    ],
    ideas: [create(IdeaViewSchema, {planNumber: 4, inRun: true})],
  });
  return {
    key: 'ssh:gpu-a',
    label: 'GPU A',
    freshness: 'live',
    status: {kind: 'live', cause: null, staleForMs: null},
    data: {
      ...emptyState,
      runs: {[alpha.id]: alpha, [beta.id]: beta},
      summaries: {
        [alpha.id]: create(RunSummarySchema, {runId: alpha.id, report}),
      },
      searches: {[alpha.id]: search},
    },
  };
};

const found = (query: string) => {
  const view = fleetView([host()], {...NO_FLEET_FILTER, query});
  return [...view.live, ...view.history];
};

describe('the loaded-runs search registry', () => {
  it('gives every closed field a positive relevance weight', () => {
    expect(Object.values(FLEET_SEARCH_REGISTRY).every(field => field.weight > 0)).toBe(true);
  });

  it.each([
    ['kv CACHE', 'title'],
    ['llama 70b', 'task model'],
    ['bursty latency', 'description'],
    ['prefix segmented', 'workstream'],
    ['reuse metadata', 'plan'],
    ['paged allocator', 'report'],
    ['codex o3', 'allowed CLI and model'],
    ['throughput tokens', 'objective'],
    ['gpu-a inference', 'machine and repository'],
    ['run-alpha', 'run id'],
  ])('finds a run by %s (%s)', query => {
    expect(found(query).map(row => row.runId)).toEqual(['run-alpha']);
  });

  it('normalizes punctuation and whitespace and requires every query token', () => {
    expect(found('  PREFIX---cache\nO3 ').map(row => row.runId)).toEqual(['run-alpha']);
    expect(found('prefix compaction')).toEqual([]);
  });

  it('leaves the fleet whole for an empty query and never invents an unmatched result', () => {
    expect(found('  ').map(row => row.runId)).toEqual(['run-alpha', 'run-beta']);
    expect(found('quantum-croissant')).toEqual([]);
  });

  it('scores a title match above the same words in a repository path', () => {
    const rows = found('');
    const alpha = rows.find(row => row.runId === 'run-alpha');
    const beta = rows.find(row => row.runId === 'run-beta');
    expect(alpha).toBeDefined();
    expect(beta).toBeDefined();
    if (alpha === undefined || beta === undefined) throw new Error('fixture rows are absent');
    expect(fleetSearchScore(alpha, 'cache')).toBeGreaterThan(
      fleetSearchScore(beta, 'storage') ?? 0,
    );
  });
});

describe('the latest structured run update', () => {
  it('prefers the newest in-run hypothesis and uses its stable facts in the activity key', () => {
    const [row] = found('run-alpha');
    expect(row?.update).toEqual({
      kind: 'hypothesis',
      title: 'Reuse block metadata',
      detail: 'Avoid rebuilding the lookup table for every batch.',
      atMs: 7_000,
    });
    expect(row?.activityKey).toContain('hypothesis:7000');
  });
});

describe('authoritative whole-history matches', () => {
  it('shows an omitted run even when the matching full search fact is not repeated in the reply', () => {
    const full = host();
    const hidden = full.data.runs['run-beta'];
    expect(hidden).toBeDefined();
    if (hidden === undefined) return;
    const bounded: FleetHostInput = {
      ...full,
      data: {
        ...full.data,
        runs: Object.fromEntries(
          Object.entries(full.data.runs).filter(([runId]) => runId !== hidden.id),
        ),
        cut: {truncated: true, runsLeftOut: 1},
      },
    };
    const answer = {
      host: full.key,
      query: 'hidden allocator experiment',
      results: [
        create(RunSearchResultSchema, {
          run: hidden,
          summary: create(RunSummarySchema, {runId: hidden.id}),
        }),
      ],
    };

    const view = fleetView(
      [bounded],
      {...NO_FLEET_FILTER, query: 'HIDDEN---allocator experiment'},
      [answer],
    );
    expect(view.live).toEqual([]);
    expect(view.history.map(row => [row.runId, row.searchOnly])).toEqual([[hidden.id, true]]);
  });

  it('ignores answers to another query and deduplicates a loaded run', () => {
    const input = host();
    const run = input.data.runs['run-beta'];
    expect(run).toBeDefined();
    if (run === undefined) return;
    const result = create(RunSearchResultSchema, {
      run,
      summary: create(RunSummarySchema, {runId: run.id}),
    });
    const filter = {...NO_FLEET_FILTER, query: 'database'};

    expect(
      fleetView([input], filter, [{host: input.key, query: 'another query', results: [result]}])
        .history,
    ).toHaveLength(1);
    const rows = fleetView([input], filter, [
      {host: input.key, query: 'DATABASE', results: [result, result]},
    ]).history;
    expect(rows.map(row => [row.runId, row.searchOnly])).toEqual([[run.id, false]]);
  });
});
