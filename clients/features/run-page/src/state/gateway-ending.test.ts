import {expect, test} from 'bun:test';
import {create, type MessageInitShape} from '@bufbuild/protobuf';
import {anyPack} from '@bufbuild/protobuf/wkt';
import {
  AgentTurnAttrsSchema,
  Change,
  DrainGoal,
  Outcome,
  RecordSchema,
  RunHeadSchema,
  RunLiveness,
  RuntimeType,
  SpanStatus,
} from '@vibesys/proto';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {foldSkeleton, type RunLivenessHint, seedSkeleton} from '@vibesys/run-skeleton';
import {SEEDS} from '@vibesys/store/testing';
import {
  headerView,
  nowView,
  projectSkeleton,
  runPageInitial,
  skeletonScreenState,
} from './index.js';

const SEED = {
  identity: {host: 'local' as const, runId: 'run-a'},
  title: {project: 'queue-rs', task: null},
  hostLabel: 'Local',
  unavailable: {},
};

const ENDINGS = [
  {name: 'finished', ends: 'done', state: {case: 'ended', value: {outcome: Outcome.FINISHED}}},
  {name: 'failed', ends: 'failed', state: {case: 'ended', value: {outcome: Outcome.FAILED}}},
  {name: 'stopped', ends: 'stopped', state: {case: 'ended', value: {outcome: Outcome.STOPPED}}},
  {
    name: 'interrupted outcome',
    // The header words an INTERRUPTED outcome 'Stopped', so its spans end stopped.
    ends: 'stopped',
    state: {case: 'ended', value: {outcome: Outcome.INTERRUPTED}},
  },
  {
    name: 'interrupted writer',
    ends: 'failed',
    state: {case: 'running', value: {}},
    hint: {epoch: 1, value: RunLiveness.INTERRUPTED},
  },
] as const;

// Never closed, whatever the hint says short of INTERRUPTED on this epoch.
const GOING = [
  {case: 'running', value: {}},
  {case: 'running', value: {hold: {reason: {case: 'operator', value: {}}}}},
  {case: 'parked', value: {}},
  {case: 'draining', value: {goal: DrainGoal.PARK}},
] as const;

const HINTS: readonly (RunLivenessHint | undefined)[] = [
  undefined,
  {epoch: 1, value: RunLiveness.ALIVE},
  {epoch: 1, value: RunLiveness.UNSPECIFIED},
  {epoch: 2, value: RunLiveness.INTERRUPTED},
];

const OPEN = new Set(['pending', 'running', 'waiting']);

const TURN_ID = 'open-turn';

/**
 * By seed: no extra span, an open agent turn with no usage, or an open turn that recorded how long
 * it took. The fixture's one open span is the last idea's workstream.
 */
function turnKind(seed: number) {
  return (['none', 'bare', 'timed'] as const)[seed % 3] ?? 'none';
}

const turnMillis = (seed: number) => 1000 + seed * 10;

function fixture(seed: number) {
  const count = 1 + (seed % 5);
  const {records} = storeRunFixture('live', count, seed);
  return {records, parent: `idea-${count - 1}`, startedAt: (count - 1) * 10 + 1};
}

function ended(seed: number, state?: (typeof GOING)[number] | (typeof ENDINGS)[number]['state']) {
  const {records, parent, startedAt} = fixture(seed);
  const all = [...records];
  const push = (body: NonNullable<MessageInitShape<typeof RecordSchema>['body']>) =>
    all.push(
      create(RecordSchema, {
        seq: BigInt(all.length + 1),
        epoch: 1,
        at: {seconds: 1700001000n + BigInt(all.length)},
        body,
      }),
    );
  if (turnKind(seed) !== 'none') {
    const usage = turnKind(seed) === 'timed' ? {durationMs: BigInt(turnMillis(seed))} : {};
    push({
      case: 'span',
      value: {
        change: Change.STARTED,
        span: {
          id: TURN_ID,
          parent,
          runtimeType: RuntimeType.OPERATION,
          name: 'Implement',
          status: SpanStatus.RUNNING,
          startedAt,
          label: {
            kind: 'agent_turn',
            title: 'Implement',
            attrs: anyPack(
              AgentTurnAttrsSchema,
              create(AgentTurnAttrsSchema, {
                agent: 'agent-b',
                role: 'implementer',
                stage: 'implement',
                provider: 'codex',
                model: 'model-a',
                usage,
              }),
            ),
          },
        },
      },
    });
  }
  if (state !== undefined) push({case: 'lifecycle', value: {state}});
  const skeleton = foldSkeleton(seedSkeleton(), all);
  return {skeleton, through: skeleton.throughSeq ?? 0n};
}

type Model = ReturnType<typeof projectSkeleton>;

function expectSpansEnded(
  seed: number,
  open: Model,
  model: Model,
  ends: 'done' | 'failed' | 'stopped',
) {
  for (const span of model.trace.spans.values()) {
    const before = open.trace.spans.get(span.id);
    if (before === undefined) throw new Error('span missing from the baseline');
    expect(span.started_at).toBe(before.started_at);
    if (!OPEN.has(before.status)) {
      expect(span.status).toBe(before.status);
      continue;
    }
    if (span.ended_at == null) throw new Error('an open span was left without an end');
    expect(span.ended_at).toBeGreaterThanOrEqual(span.started_at);
    if (span.id === TURN_ID && turnKind(seed) === 'timed') {
      expect(span.status).toBe('done');
      expect(span.ended_at).toBeCloseTo(span.started_at + turnMillis(seed) / 1000, 6);
    } else {
      expect(span.status).toBe(ends);
    }
  }
}

function lastIdeaOutcome(model: Model) {
  return model.candidates.candidates.at(-1)?.outcome;
}

for (const seed of SEEDS.slice(0, 24)) {
  test(`property: every ending ends every span the run left open (seed ${seed})`, () => {
    for (const ending of ENDINGS) {
      const {skeleton, through} = ended(seed, ending.state);
      const hint = 'hint' in ending ? ending.hint : undefined;
      const base = ended(seed);
      const open = projectSkeleton(base.skeleton, base.through);
      const model = projectSkeleton(skeleton, through, '', {hint});
      const spans = [...model.trace.spans.values()];
      expect(spans.every(span => !OPEN.has(span.status))).toBe(true);
      expect(model.candidates.candidates.every(idea => idea.outcome !== 'running')).toBe(true);
      expect(nowView(model)).toEqual([]);
      expect(model.agents.agents.every(agent => agent.status !== 'running')).toBe(true);
      expectSpansEnded(seed, open, model, ending.ends);
      if (turnKind(seed) !== 'none') {
        expect(open.trace.spans.get(TURN_ID)?.status).toBe('running');
        expect(open.agents.agents.some(agent => agent.status === 'running')).toBe(true);
      }
      for (const idea of open.candidates.candidates) {
        if (idea.outcome === 'running') continue;
        expect(model.candidates.candidates.find(item => item.id === idea.id)?.outcome).toBe(
          idea.outcome,
        );
      }
    }
  });

  test(`property: a run still going closes nothing (seed ${seed})`, () => {
    for (const state of [undefined, ...GOING]) {
      for (const hint of HINTS) {
        const {skeleton, through} = ended(seed, state);
        const model = projectSkeleton(skeleton, through, '', {hint});
        expect(lastIdeaOutcome(model)).toBe('running');
      }
    }
  });

  test(`property: a run with no lifecycle record closes nothing (seed ${seed})`, () => {
    const records = fixture(seed)
      .records.filter(record => record.body.case !== 'lifecycle')
      .map((record, index) => create(RecordSchema, {...record, seq: BigInt(index + 1)}));
    const skeleton = foldSkeleton(seedSkeleton(), records);
    for (const hint of HINTS) {
      const model = projectSkeleton(skeleton, skeleton.throughSeq ?? 0n, '', {hint});
      expect(lastIdeaOutcome(model)).toBe('running');
    }
  });

  test(`property: scrubbing back on an ended run closes nothing (seed ${seed})`, () => {
    for (const ending of ENDINGS) {
      const {skeleton, through} = ended(seed, ending.state);
      const hint = 'hint' in ending ? ending.hint : undefined;
      const past = projectSkeleton(skeleton, through - 1n, '', {hint});
      expect(past).toEqual(projectSkeleton(skeleton, through - 1n));
      expect(lastIdeaOutcome(past)).toBe('running');
    }
  });

  test(`property: an ending is not applied while history loads (seed ${seed})`, () => {
    for (const ending of ENDINGS) {
      const {skeleton, through} = ended(seed, ending.state);
      const hint = 'hint' in ending ? ending.hint : undefined;
      const loading = projectSkeleton(skeleton, through, '', {
        hint,
        progress: {phase: 'history', through, committed: through + 5n},
      });
      expect(lastIdeaOutcome(loading)).toBe('running');
      const complete = projectSkeleton(skeleton, through, '', {
        hint,
        progress: {phase: 'complete'},
      });
      expect(lastIdeaOutcome(complete)).not.toBe('running');
    }
  });
}

test('the header and the ideas agree on an interrupted writer', () => {
  const ending = ENDINGS[4];
  for (const seed of SEEDS.slice(0, 24)) {
    const {skeleton, through} = ended(seed, ending.state);
    const model = projectSkeleton(skeleton, through, '', {hint: ending.hint});
    const state = skeletonScreenState(SEED, skeleton, model, runPageInitial(SEED), ending.hint);
    expect(headerView(state).status.label).toBe('Interrupted');
    expect(model.candidates.candidates.some(idea => idea.outcome === 'running')).toBe(false);
  }
});

test('a head ahead of the loaded records closes nothing; a head at them closes', () => {
  for (const seed of SEEDS.slice(0, 24)) {
    const {skeleton, through} = ended(seed);
    const hintAt = (committedSeq: bigint): RunLivenessHint => ({
      epoch: 1,
      value: RunLiveness.ALIVE,
      head: create(RunHeadSchema, {
        epoch: 1,
        committedSeq,
        lifecycle: {state: {case: 'ended', value: {outcome: Outcome.FAILED}}},
      }),
    });
    expect(
      lastIdeaOutcome(projectSkeleton(skeleton, through, '', {hint: hintAt(through + 1n)})),
    ).toBe('running');
    expect(
      lastIdeaOutcome(projectSkeleton(skeleton, through, '', {hint: hintAt(through)})),
    ).not.toBe('running');
  }
});
