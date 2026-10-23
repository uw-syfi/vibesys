/** One durable turn fact supplies the header, Usage tiles, totals and agent rows. */
import {expect, test} from 'bun:test';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {anyPack, anyUnpack} from '@bufbuild/protobuf/wkt';
import {
  AgentTurnAttrsSchema,
  RecordSchema,
  type Record as StoreRecord,
  TokenUsageSchema,
} from '@vibesys/proto';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {foldSkeleton, type Skeleton, seedSkeleton} from '@vibesys/run-skeleton';
import type {TokenTally} from '@vibesys/run-state';
import {totalUsage} from '@vibesys/run-state/usage';
import {Seeded, seedsAtDepth} from '@vibesys/store/testing';
import {
  headerView,
  projectSkeleton,
  runPageInitial,
  skeletonScreenState,
  tokensByAgent,
  tokensReading,
  USAGE_CONTROLS_DEFAULT,
  usageRecords,
  usageView,
} from './index.js';

const SEED = {
  identity: {host: 'local' as const, runId: 'run-a'},
  title: {project: 'project-a', task: null},
  hostLabel: 'Local',
  unavailable: {},
};
const SEEDS = seedsAtDepth([1, 7, 23, 42, 97, 101, 211, 300]);

function generatedRecords(random: Seeded, seed: number) {
  const source = storeRunFixture('short', 1 + random.int(6), seed).records;
  for (const record of source) {
    if (record.body.case !== 'span' || record.body.value.span?.label?.kind !== 'agent_turn')
      continue;
    const label = record.body.value.span.label;
    if (!label.attrs) throw new Error('fixture has no attrs');
    const attrs = anyUnpack(label.attrs, AgentTurnAttrsSchema);
    if (!attrs) throw new Error('fixture has wrong attrs type');
    const input = random.int(500_000);
    const output = random.int(100_000);
    // Optional fields include zero, absent and nonzero figures. Absence stays unknown.
    attrs.usage = create(TokenUsageSchema, {
      input: BigInt(input),
      output: BigInt(output),
      ...(random.bool() ? {cacheRead: BigInt(random.int(input + 1))} : {}),
      ...(random.bool() ? {cacheCreation: BigInt(random.int(1000))} : {}),
      ...(random.bool() ? {reasoning: BigInt(random.int(output + 1))} : {}),
    });
    label.attrs = anyPack(AgentTurnAttrsSchema, attrs);
  }
  return source;
}

function reportOf(record: StoreRecord): TokenTally[] {
  const span = record.body.case === 'span' ? record.body.value.span : undefined;
  if (span?.label?.kind !== 'agent_turn' || !span.label.attrs) return [];
  const usage = anyUnpack(span.label.attrs, AgentTurnAttrsSchema)?.usage;
  if (!usage) throw new Error('fixture has no usage');
  return [
    {
      input: Number(usage.input),
      output: Number(usage.output),
      cacheRead: usage.cacheRead === undefined ? null : Number(usage.cacheRead),
      cacheWrite: usage.cacheCreation === undefined ? null : Number(usage.cacheCreation),
      reasoning: usage.reasoning === undefined ? null : Number(usage.reasoning),
    },
  ];
}

function expectedTally(reports: readonly TokenTally[]): TokenTally {
  const sum = (key: keyof TokenTally) => {
    const values = reports.map(report => report[key]).filter(value => value !== null);
    return values.length === 0 ? null : values.reduce((total, value) => total + value, 0);
  };
  return {
    input: sum('input'),
    output: sum('output'),
    cacheRead: sum('cacheRead'),
    cacheWrite: sum('cacheWrite'),
    reasoning: sum('reasoning'),
  };
}

function assertNumbers(skeleton: Skeleton, reports: readonly TokenTally[]) {
  const expected = expectedTally(reports);
  const model = projectSkeleton(skeleton, skeleton.throughSeq ?? 0n);
  const screen = skeletonScreenState(SEED, skeleton, model, runPageInitial(SEED));
  const core = screen.run.core;
  if (!core) throw new Error('native prefix has no core');
  expect(core.tokens.total).toEqual(expected);
  expect(headerView(screen).spent).toEqual(tokensReading(expected));
  const records = usageRecords(screen);
  expect(totalUsage(records).tokens).toEqual(expected);
  const view = usageView(records, USAGE_CONTROLS_DEFAULT, {
    ideaOfSpan: new Map(),
    ideas: new Map(),
    models: new Map(),
  });
  expect(view.tiles[1]?.title).toBe(tokensReading(expected)?.title ?? '');
  expect(view.rows.at(-1)?.turns ?? '0').toBe(String(reports.length));
  const byAgent = tokensByAgent(model.trace.spans, core.tokens);
  if (reports.length > 0) expect(byAgent.get('agent-a')).toEqual(expected);
}

test('every native prefix displays exactly its reported accounting, across page boundaries', () => {
  for (const seed of SEEDS) {
    const random = new Seeded(seed);
    const source = generatedRecords(random, seed);
    const reports: TokenTally[] = [];
    let skeleton = seedSkeleton();
    let position = 0;
    while (position < source.length) {
      const end = Math.min(source.length, position + 1 + random.int(7));
      const page = source
        .slice(position, end)
        .map(record => fromBinary(RecordSchema, toBinary(RecordSchema, record)));
      reports.push(...page.flatMap(reportOf));
      skeleton = foldSkeleton(skeleton, page);
      position = end;
      assertNumbers(skeleton, reports);
    }
  }
});
