/**
 * The sessions of a run, read from the epoch headers and the times its records state: one session
 * per start of the run, from the first time it recorded to the last, however long it stood stopped
 * between them, and at every prefix of the run.
 */
import {describe, expect, test} from 'bun:test';
import {clone, create, type MessageInitShape} from '@bufbuild/protobuf';
import {Change, RecordSchema, RuntimeType, SpanStatus} from '@vibesys/proto';
import {storeRunFixture} from '@vibesys/run-fixtures';
import {foldSkeleton, seedSkeleton, seekSkeleton} from '@vibesys/run-skeleton';
import {clockThrough, readSkeleton} from '@vibesys/run-state/skeleton';
import fc from 'fast-check';

type Records = ReturnType<typeof storeRunFixture>['records'];
type Body = NonNullable<MessageInitShape<typeof RecordSchema>['body']>;

/** The epoch header of a run: the one every later start of it repeats, numbered. */
const [HEADER] = storeRunFixture('short', 1, 1).records;

/** One session as generated: how long it worked, with how many spans, and how long the run stood before it. */
interface Part {
  readonly work: number;
  readonly spans: number;
  readonly stopped: number;
}

const parts: fc.Arbitrary<readonly Part[]> = fc.array(
  fc.record({
    work: fc.integer({min: 10, max: 5000}),
    spans: fc.integer({min: 1, max: 4}),
    stopped: fc.integer({min: 0, max: 90000}),
  }),
  {minLength: 1, maxLength: 5},
);

/**
 * The records of a run that was started once and resumed after each of `parts`' stops: each start a
 * header, then spans that began and ended inside its working time. `downtime` says whether the run
 * clock ran on while the run stood stopped (an older writer's) or stopped with it.
 */
function recordsOf(parts: readonly Part[], downtime: boolean) {
  const records: Records[number][] = [];
  const expected: {from: number; to: number}[] = [];
  let clock = 0;
  const push = (body: Body): void => {
    records.push(create(RecordSchema, {seq: BigInt(records.length + 1), epoch: expected.length, body}));
  };
  for (const part of parts) {
    if (downtime || expected.length === 0) clock += part.stopped;
    expected.push({from: clock, to: clock + part.work});
    const header = clone(RecordSchema, HEADER as Records[number]);
    if (header.body.case === 'epochStarted') header.body.value.epoch = expected.length;
    records.push(create(RecordSchema, {...header, seq: BigInt(records.length + 1), epoch: expected.length}));
    for (let span = 0; span < part.spans; span += 1) {
      const started = clock + Math.floor((span * part.work) / part.spans / 2);
      const ended = span === part.spans - 1 ? clock + part.work : started + 1;
      push({
        case: 'span',
        value: {
          change: Change.ENDED,
          span: {
            id: `s${records.length}`,
            runtimeType: RuntimeType.OPERATION,
            name: 'work',
            status: SpanStatus.DONE,
            startedAt: started,
            endedAt: ended,
          },
        },
      });
    }
    clock += part.work;
  }
  return {records, expected};
}

function sessionsAt(records: readonly Records[number][], through: number) {
  const skeleton = foldSkeleton(seedSkeleton(), records.slice(0, through));
  return readSkeleton(skeleton, seekSkeleton(skeleton, skeleton.throughSeq ?? 0n)).sessions;
}

describe('the sessions of a run', () => {
  for (const downtime of [false, true]) {
    test(`are one per start, each from its first time to its last (the clock ${downtime ? 'runs on' : 'stops'} while stopped)`, () => {
      fc.assert(
        fc.property(parts, each => {
          const {records, expected} = recordsOf(each, downtime);
          expect(sessionsAt(records, records.length)).toEqual(expected);
        }),
        {numRuns: 12},
      );
    });
  }

  test('at every prefix are the sessions of the whole run so far: only the last one grows', () => {
    fc.assert(
      fc.property(parts, each => {
        const {records, expected} = recordsOf(each, true);
        for (let length = 1; length <= records.length; length += 3) {
          const sessions = sessionsAt(records, length);
          expect(sessions.length).toBeLessThanOrEqual(expected.length);
          for (const [at, session] of sessions.entries()) {
            const whole = expected[at];
            expect(session.from).toBe(whole?.from as number);
            expect(session.to).toBeLessThanOrEqual(whole?.to as number);
            if (at < sessions.length - 1) expect(session.to).toBe(whole?.to as number);
          }
        }
      }),
      {numRuns: 12},
    );
  });

  test('a run started once is one session', () => {
    const {records} = storeRunFixture('short', 3, 1);
    expect(sessionsAt(records, records.length)).toHaveLength(1);
  });
});

describe('where the time bar stands on the run clock', () => {
  test('is the latest time stated by each position, never going back', () => {
    fc.assert(
      fc.property(parts, each => {
        const {records} = recordsOf(each, true);
        const skeleton = foldSkeleton(seedSkeleton(), records);
        const seqs = records.map((_, at) => at + 1);
        const times = clockThrough(skeleton, seqs);
        let reached = Number.NEGATIVE_INFINITY;
        for (const time of times) {
          if (time === null) continue;
          expect(time).toBeGreaterThanOrEqual(reached);
          reached = time;
        }
        const last = times.at(-1);
        expect(last).toBe(Math.max(...records.flatMap(r => (r.body.case === 'span' ? [r.body.value.span?.endedAt ?? 0] : [0]))));
      }),
      {numRuns: 10},
    );
  });
});
