/**
 * What a running idea is doing, read from the run's records the way the gateway path reads them:
 * the activity the run wrote on the turn's span (`AgentTurnAttrs.activity`), else the step it runs
 * or waits after. Its age is measured to the run's newest news, never the reader's clock.
 */
import {describe, expect, test} from 'bun:test';
import {create, type MessageInitShape} from '@bufbuild/protobuf';
import {anyPack} from '@bufbuild/protobuf/wkt';
import {
  AgentTurnAttrsSchema,
  Change,
  GateAttrsSchema,
  GateStage,
  GateSubject,
  RecordSchema,
  RuntimeType,
  SpanStatus,
} from '@vibesys/proto';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {foldSkeleton, seedSkeleton} from '@vibesys/run-skeleton';
import {SEEDS} from '@vibesys/store/testing';
import {
  activityAge,
  activityLine,
  activityNow,
  activityWords,
  type IdeaActivity,
  ideaActivities,
  projectSkeleton,
  QUIET_AFTER,
  summaryLine,
} from './index.js';

type Body = NonNullable<MessageInitShape<typeof RecordSchema>['body']>;

const TOOLS = ['Edit', 'Bash', 'mcp__vibesys__measure'] as const;

/** The live fixture's last idea runs: its workstream began at `startedAt` and has no step yet. */
function running(seed: number, bodies: (parent: string, startedAt: number) => readonly Body[]) {
  const count = 1 + (seed % 4);
  const {records} = storeRunFixture('live', count, seed);
  const parent = `idea-${count - 1}`;
  const all = [...records];
  for (const body of bodies(parent, (count - 1) * 10)) {
    all.push(
      create(RecordSchema, {
        seq: BigInt(all.length + 1),
        epoch: 1,
        at: {seconds: 1700001000n + BigInt(all.length)},
        body,
      }),
    );
  }
  const skeleton = foldSkeleton(seedSkeleton(), all);
  const model = projectSkeleton(skeleton, skeleton.throughSeq ?? 0n);
  return {model, idea: parent};
}

function turn(
  parent: string,
  startedAt: number,
  activity: MessageInitShape<typeof AgentTurnAttrsSchema>['activity'],
  ended = false,
): Body {
  return {
    case: 'span',
    value: {
      change: ended ? Change.ENDED : Change.UPDATED,
      span: {
        id: `${parent}-turn`,
        parent,
        runtimeType: RuntimeType.OPERATION,
        name: 'Implement',
        status: ended ? SpanStatus.DONE : SpanStatus.RUNNING,
        startedAt: startedAt + 1,
        ...(ended ? {endedAt: startedAt + 3} : {}),
        label: {
          kind: 'agent_turn',
          title: 'implementer',
          attrs: anyPack(
            AgentTurnAttrsSchema,
            create(AgentTurnAttrsSchema, {
              agent: 'agent-a',
              role: 'implementer',
              stage: 'implement',
              ...(activity === undefined ? {} : {activity}),
            }),
          ),
        },
      },
    },
  };
}

function gate(parent: string, startedAt: number): Body {
  return {
    case: 'span',
    value: {
      change: Change.STARTED,
      span: {
        id: `${parent}-gate`,
        parent,
        runtimeType: RuntimeType.OPERATION,
        name: 'Benchmark',
        status: SpanStatus.RUNNING,
        startedAt: startedAt + 4,
        label: {
          kind: 'gate',
          title: 'correctness + benchmark',
          attrs: anyPack(
            GateAttrsSchema,
            create(GateAttrsSchema, {stage: GateStage.BENCHMARK, subject: GateSubject.CANDIDATE}),
          ),
        },
      },
    },
  };
}

/** By seed: the activity the run wrote (null: none, as an older run), and what the row says. */
function writtenBy(seed: number) {
  if (seed % 5 === 0) return {written: null, what: 'Working'};
  const tool = seed % 4 === 0 ? undefined : TOOLS[seed % TOOLS.length];
  const todo = seed % 3 === 0 ? undefined : `Pad the ring ${seed}`;
  const what = tool !== undefined ? `Using ${tool}` : (todo ?? 'Working');
  return {written: {...(tool ? {tool} : {}), ...(todo ? {todo} : {})}, what};
}

describe('what a running idea is doing', () => {
  test('a turn says the tool it runs, else its todo, else that it works, as the run wrote it', () => {
    for (const seed of SEEDS) {
      const {written, what} = writtenBy(seed);
      const {model, idea} = running(seed, (parent, startedAt) => [
        turn(parent, startedAt, written === null ? undefined : {...written, since: startedAt + 2}),
      ]);
      const activity = ideaActivities(model).get(idea);
      expect(activity === undefined ? null : activityWords(activity)).toBe(`Implementer · ${what}`);
      expect(activity?.kind).toBe('turn');
    }
  });

  test('a gate measures; after a turn the idea waits; before any step it starts', () => {
    for (const seed of SEEDS.slice(0, 40)) {
      const cases = [
        {
          bodies: (parent: string, at: number) => [gate(parent, at)],
          says: 'Measuring · correctness + benchmark',
        },
        {
          bodies: (parent: string, at: number) => [
            turn(parent, at, {tool: 'Edit', since: at + 2}, true),
          ],
          says: 'Waiting after Implementer',
        },
        {bodies: () => [], says: 'Starting'},
      ];
      for (const each of cases) {
        const {model, idea} = running(seed, each.bodies);
        const activity = ideaActivities(model).get(idea);
        expect(activity === undefined ? null : activityWords(activity)).toBe(each.says);
      }
    }
  });

  test('ideas that do not run say nothing', () => {
    for (const seed of SEEDS.slice(0, 40)) {
      const {model, idea} = running(seed, () => []);
      expect([...ideaActivities(model).keys()]).toEqual([idea]);
    }
  });
});

function activity(kind: IdeaActivity['kind'], since: number): IdeaActivity {
  return {kind, step: null, tool: kind === 'turn' ? 'Edit' : null, todo: null, since};
}

describe('how long since its news', () => {
  test('recent news says how long ago; past QUIET_AFTER it is quiet; a gate is running', () => {
    for (const seed of SEEDS) {
      const since = seed * 7.5;
      const age = (seed * 13) % (QUIET_AFTER * 3);
      for (const kind of ['turn', 'waiting', 'starting'] as const) {
        const said = activityAge(activity(kind, since), since + age);
        expect(said?.quiet).toBe(age >= QUIET_AFTER);
        expect(said?.text).toMatch(age >= QUIET_AFTER ? /^quiet for \d/ : / ago$/);
      }
      expect(activityAge(activity('gate', since), since + age)).toMatchObject({quiet: false});
      expect(activityAge(activity('gate', since), since + age)?.text).toMatch(/^running \d/);
    }
  });

  test('the run’s newest news is the later of its clock and any idea’s latest change', () => {
    for (const seed of SEEDS) {
      const changes = new Map(
        Array.from({length: seed % 5}, (_, index) => [
          `idea-${index}`,
          activity('turn', ((seed + index) * 37) % 500),
        ]),
      );
      const clock = seed % 7 === 0 ? null : (seed * 11) % 600;
      const newest = Math.max(
        ...(clock === null ? [] : [clock]),
        ...[...changes.values()].map(each => each.since),
      );
      expect(activityNow(changes, clock)).toBe(Number.isFinite(newest) ? newest : null);
    }
  });

  test('without a run clock the line is only what it does', () => {
    expect(activityLine(activity('turn', 5), null)).toBe('Using Edit');
    expect(activityLine(activity('turn', 5), 17)).toBe('Using Edit · 12s ago');
  });
});

describe('what an implementer reported, as one line', () => {
  test.each([
    [
      'Swapped the mutex for a per-slot sequence ring.',
      'Swapped the mutex for a per-slot sequence ring',
    ],
    ['- Padded head and tail. Then measured again.\n\nMore text.', 'Padded head and tail'],
    ['\n\n## Result\nKept the old path', 'Result'],
    ['**Bold** start, no period', '**Bold** start, no period'],
    ['', ''],
  ])('%p reads %p', (summary, line) => {
    expect(summaryLine(summary)).toBe(line);
  });

  test('the line is one line and never ends in a period', () => {
    for (const seed of SEEDS) {
      const text = Array.from(
        {length: 1 + (seed % 4)},
        (_, index) => `${'- '.repeat(index % 2)}Sentence ${seed}.${index}. Next part`,
      ).join(seed % 2 === 0 ? '\n' : '\n\n');
      const line = summaryLine(text);
      expect(line).not.toContain('\n');
      expect(line.endsWith('.')).toBe(false);
    }
  });
});
