/** A log as the run recovery redesign writes it: core's bookkeeping records among the ordinary ones. */
import {create, type MessageInitShape} from '@bufbuild/protobuf';
import type {Record as StoreRecord} from '@vibesys/proto';
import {InterruptionReason, RecordSchema, StepCancelReason} from '@vibesys/proto';
import type {CoreBodyCase} from '@vibesys/run-state/records';

type Body = NonNullable<MessageInitShape<typeof RecordSchema>['body']>;
/** The canonical JSON of a core value, as bytes (ASCII only). */
const json = (value: unknown): Uint8Array =>
  Uint8Array.from(JSON.stringify(value), char => char.charCodeAt(0));

/** One body of each core kind; a core kind added to the store schema fails type-checking here. */
export const CORE_BODIES = {
  runStarted: {
    case: 'runStarted',
    value: {schemaVersion: 1, state: json({run: {run_id: 'run-1'}})},
  },
  coreSnapshot: {
    case: 'coreSnapshot',
    value: {
      schemaVersion: 1,
      envelopeVersion: 1,
      state: json({run: {run_id: 'run-1'}}),
      inputs: [json({type: 'clock'})],
      strategy: json({journal: []}),
      wakes: [30],
      held: json([]),
    },
  },
  strategyAdvanced: {
    case: 'strategyAdvanced',
    value: {
      schemaVersion: 1,
      proposals: [json({type: 'proposal'})],
      strategy: {case: 'checkpoint', value: json({journal: []})},
      wakes: [30],
      delivered: [],
      held: json([]),
    },
  },
  coreInput: {case: 'coreInput', value: {schemaVersion: 1, event: json({type: 'clock'})}},
  stepStarted: {
    case: 'stepStarted',
    value: {
      tag: 'plan-1',
      attempt: 1,
      schemaVersion: 1,
      request: json({tag: 'plan-1'}),
      deadlineAt: 60,
    },
  },
  stepFinished: {
    case: 'stepFinished',
    value: {
      tag: 'plan-1',
      attempt: 1,
      schemaVersion: 1,
      result: {case: 'cancelled', value: StepCancelReason.DEADLINE},
    },
  },
  attemptInterrupted: {
    case: 'attemptInterrupted',
    value: {tag: 'plan-1', attempt: 1, reason: InterruptionReason.PAUSE},
  },
  runEnded: {case: 'runEnded', value: {schemaVersion: 1, result: json({outcome: 'finished'})}},
} as const satisfies Record<CoreBodyCase, Body>;

/** A step whose first attempt a pause interrupted and whose second attempt finished it. */
export const INTERRUPTED_THEN_RERUN: readonly Body[] = [
  {
    case: 'stepStarted',
    value: {
      tag: 'plan-1',
      attempt: 1,
      schemaVersion: 1,
      request: json({tag: 'plan-1'}),
      deadlineAt: 60,
    },
  },
  CORE_BODIES.attemptInterrupted,
  {
    case: 'stepStarted',
    value: {
      tag: 'plan-1',
      attempt: 2,
      schemaVersion: 1,
      request: json({tag: 'plan-1'}),
      deadlineAt: 60,
    },
  },
  {
    case: 'stepFinished',
    value: {
      tag: 'plan-1',
      attempt: 2,
      schemaVersion: 1,
      result: {case: 'finished', value: json({type: 'observation'})},
    },
  },
];

/**
 * The records with `core[i]` core records written before the i-th of them, and the rest after the
 * last, the whole log renumbered 1, 2, 3, ... as a writer numbers it. The core bodies are taken in
 * turn, from every kind unless `bodies` says which.
 */
export function withCoreRecords(
  records: readonly StoreRecord[],
  core: readonly number[],
  bodies: readonly Body[] = Object.values(CORE_BODIES),
): readonly StoreRecord[] {
  const log: StoreRecord[] = [];
  let next = 0;
  const push = (record: StoreRecord | undefined, body: Body | undefined): void => {
    const seq = BigInt(log.length + 1);
    if (record) log.push(create(RecordSchema, {...record, seq}));
    else if (body) log.push(create(RecordSchema, {seq, epoch: 1, at: {seconds: seq}, body}));
  };
  records.forEach((record, index) => {
    for (let count = core[index] ?? 0; count > 0; count -= 1)
      push(undefined, bodies[next++ % bodies.length]);
    push(record, undefined);
  });
  for (let count = core[records.length] ?? 0; count > 0; count -= 1)
    push(undefined, bodies[next++ % bodies.length]);
  return log;
}
