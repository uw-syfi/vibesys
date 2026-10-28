import {expect, test} from 'bun:test';
import {create} from '@bufbuild/protobuf';
import {DrainGoal, Outcome, RecordSchema, RunLiveness} from '@vibesys/proto';
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
  {name: 'finished', state: {case: 'ended', value: {outcome: Outcome.FINISHED}}},
  {name: 'failed', state: {case: 'ended', value: {outcome: Outcome.FAILED}}},
  {name: 'stopped', state: {case: 'ended', value: {outcome: Outcome.STOPPED}}},
  {name: 'interrupted outcome', state: {case: 'ended', value: {outcome: Outcome.INTERRUPTED}}},
  {
    name: 'interrupted writer',
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

function fixture(seed: number) {
  const {records} = storeRunFixture('live', 1 + (seed % 5), seed);
  return records;
}

function ended(seed: number, state?: (typeof GOING)[number] | (typeof ENDINGS)[number]['state']) {
  const records = fixture(seed);
  const last = records[records.length - 1];
  if (last === undefined) throw new Error('fixture has records');
  const all =
    state === undefined
      ? records
      : [
          ...records,
          create(RecordSchema, {
            seq: last.seq + 1n,
            epoch: 1,
            at: {seconds: 1700001000n},
            body: {case: 'lifecycle', value: {state}},
          }),
        ];
  const skeleton = foldSkeleton(seedSkeleton(), all);
  return {skeleton, through: skeleton.throughSeq ?? 0n};
}

function lastIdeaOutcome(model: ReturnType<typeof projectSkeleton>) {
  return model.candidates.candidates.at(-1)?.outcome;
}

for (const seed of SEEDS.slice(0, 24)) {
  test(`property: every ending ends every span the run left open (seed ${seed})`, () => {
    for (const ending of ENDINGS) {
      const {skeleton, through} = ended(seed, ending.state);
      const hint = 'hint' in ending ? ending.hint : undefined;
      const open = projectSkeleton(skeleton, through);
      const model = projectSkeleton(skeleton, through, '', {hint});
      const spans = [...model.trace.spans.values()];
      expect(spans.every(span => !OPEN.has(span.status))).toBe(true);
      expect(model.candidates.candidates.every(idea => idea.outcome !== 'running')).toBe(true);
      expect(nowView(model)).toEqual([]);
      expect(model.agents.agents.every(agent => agent.status !== 'running')).toBe(true);
      for (const span of spans) {
        const before = open.trace.spans.get(span.id);
        expect(before).toBeDefined();
        expect(span.started_at === before?.started_at).toBe(true);
        if (span.ended_at != null && span.started_at != null) {
          expect(span.ended_at >= span.started_at).toBe(true);
        }
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

  test(`property: scrubbing back on an ended run closes nothing (seed ${seed})`, () => {
    for (const ending of ENDINGS) {
      const {skeleton, through} = ended(seed, ending.state);
      const hint = 'hint' in ending ? ending.hint : undefined;
      expect(projectSkeleton(skeleton, through - 1n, '', {hint})).toEqual(
        projectSkeleton(skeleton, through - 1n),
      );
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
