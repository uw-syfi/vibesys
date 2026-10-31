/**
 * Generated records of a run that was started and resumed: the skeletons the tests of the run's
 * sessions and of its running time read.
 */
import {clone, create, type MessageInitShape} from '@bufbuild/protobuf';
import {Change, RecordSchema, RuntimeType, SpanStatus} from '@vibesys/proto';
import {storeRunFixture} from '@vibesys/run-fixtures';
import fc from 'fast-check';

export type Records = ReturnType<typeof storeRunFixture>['records'];
type Body = NonNullable<MessageInitShape<typeof RecordSchema>['body']>;

/** The epoch header of a run: the one every later start of it repeats, numbered. */
const [HEADER] = storeRunFixture('short', 1, 1).records;

/** One session as generated: how long it worked, with how many spans, and how long the run stood before it. */
export interface Part {
  readonly work: number;
  readonly spans: number;
  readonly stopped: number;
}

export const parts: fc.Arbitrary<readonly Part[]> = fc.array(
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
export function recordsOf(parts: readonly Part[], downtime: boolean) {
  const records: Records[number][] = [];
  const expected: {from: number; to: number}[] = [];
  let clock = 0;
  const push = (body: Body): void => {
    records.push(
      create(RecordSchema, {seq: BigInt(records.length + 1), epoch: expected.length, body}),
    );
  };
  for (const part of parts) {
    if (downtime || expected.length === 0) clock += part.stopped;
    expected.push({from: clock, to: clock + part.work});
    const header = clone(RecordSchema, HEADER as Records[number]);
    if (header.body.case === 'epochStarted') header.body.value.epoch = expected.length;
    records.push(
      create(RecordSchema, {...header, seq: BigInt(records.length + 1), epoch: expected.length}),
    );
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
