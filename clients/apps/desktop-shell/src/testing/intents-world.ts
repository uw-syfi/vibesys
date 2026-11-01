/**
 * What the host answers a run's Pause, Pause now, guidance and Resume, as bytes, and the records
 * its journal then holds: the data half of the run-intents journeys. A live store run, the posted
 * requests kept in order, a journal outcome per request, and the writer's liveness observations. The
 * browser journeys deliver them through a page (`e2e/run-intents.spec.ts`), the DOM tests through
 * the window's Fake gateway (`run-intents.dom.test.tsx`).
 */
import {clone, create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  Change,
  HostCallRequestSchema,
  HostCallResponseSchema,
  IntentAction,
  LifecycleSchema,
  type PostIntentRequest,
  RecordSchema,
  RelaunchRefusal,
  RunEntrySchema,
  type RunLiveness,
  SpanStatus,
  type Record as StoreRecord,
  TailResponseSchema,
} from '@vibesys/proto';
import {intentReplay, type StoreRunFixture, storeRunFixture} from '@vibesys/run-fixtures';
import {type WireFixture, wire} from './gateway-fixture.js';

function appliedState(action: IntentAction) {
  return create(LifecycleSchema, {
    state:
      action === IntentAction.PAUSE_NOW
        ? {case: 'parked', value: {}}
        : {case: 'draining', value: {}},
  }).state;
}

/** A tail reply holding `records`, as the bytes the Fake gateway appends. */
export function tailOf(records: readonly StoreRecord[]): number[] {
  const sequence = records.at(-1)?.seq;
  if (sequence === undefined) throw new Error('records required');
  return [
    ...toBinary(
      TailResponseSchema,
      create(TailResponseSchema, {
        records: records.map(record => toBinary(RecordSchema, record)),
        throughSeq: sequence,
      }),
    ),
  ];
}

/** The writer's observation: the host says the run is `liveness` in `epoch`. */
export function observationOf(epoch: number, liveness: RunLiveness): number[] {
  return [...toBinary(RunEntrySchema, create(RunEntrySchema, {liveness, head: {epoch}}))];
}

/** The records that end the spans a run left open, as the run's next epoch does. */
export function finishOpenSpans(
  records: readonly StoreRecord[],
  next: () => bigint,
): StoreRecord[] {
  const spans = new Map(
    records.flatMap(record =>
      record.body.case === 'span' && record.body.value.span
        ? [[record.body.value.span.id, record.body.value.span] as const]
        : [],
    ),
  );
  return [...spans.values()]
    .filter(span => span.endedAt === undefined)
    .map(span =>
      create(RecordSchema, {
        seq: next(),
        epoch: 2,
        body: {
          case: 'span',
          value: {
            change: Change.ENDED,
            span: {...span, status: SpanStatus.STOPPED, endedAt: Math.max(10, span.startedAt)},
          },
        },
      }),
    );
}

export interface IntentsWorld {
  readonly fixture: StoreRunFixture;
  /** What the Fake gateway serves: the live run, its history held back when `history` is pending. */
  readonly input: WireFixture;
  /** Every intent the window posted, in order. */
  readonly requests: {host: string; request: PostIntentRequest}[];
  /** The host's answer to a call of the window: the admission of an intent, or a relaunch. */
  readonly postIntent: (host: string, bytes: number[]) => number[];
  /** The journal's outcome of the `index`th posted request, as the tail the gateway appends. */
  readonly outcome: (index: number, result: 'applied' | 'refused') => number[];
  /** The next sequence number of the journal. */
  readonly next: () => bigint;
}

/** A live run whose window posts intents to a host that admits them and writes their outcomes. */
export function intentsWorld(
  options: {amendment?: boolean; history?: 'pending'} = {},
): IntentsWorld {
  const source = storeRunFixture('live');
  const fixture = {...source, records: [...source.records]};
  if (options.amendment) {
    const header = fixture.records.find(record => record.body.case === 'epochStarted');
    if (header?.body.case !== 'epochStarted') throw new Error('fixture header required');
    fixture.records.push(
      create(RecordSchema, {
        seq: (fixture.records.at(-1)?.seq ?? 0n) + 1n,
        epoch: header.epoch,
        at: {seconds: 1700000200n},
        body: {case: 'declarationAmended', value: header.body.value},
      }),
    );
  }
  const requests: {host: string; request: PostIntentRequest}[] = [];
  let sequence = fixture.records.at(-1)?.seq ?? 0n;
  const input = wire(fixture);
  if (options.history === 'pending') {
    input.tail = [
      ...toBinary(
        TailResponseSchema,
        create(TailResponseSchema, {
          throughSeq: fixture.records[fixture.initialCount - 1]?.seq ?? 0n,
        }),
      ),
    ];
  }
  return {
    fixture,
    input,
    requests,
    next: () => ++sequence,
    postIntent: (host, bytes) => {
      const envelope = fromBinary(HostCallRequestSchema, new Uint8Array(bytes));
      if (envelope.method.case === 'relaunch')
        return [
          ...toBinary(
            HostCallResponseSchema,
            create(HostCallResponseSchema, {
              result: {case: 'relaunch', value: {outcome: {case: 'epoch', value: 2n}}},
            }),
          ),
        ];
      if (envelope.method.case !== 'postIntent') throw new Error('Expected PostIntent');
      requests.push({host, request: envelope.method.value});
      const replay = intentReplay('pending', envelope.method.value.requestId);
      return [...toBinary(HostCallResponseSchema, replay.admission)];
    },
    outcome: (index, result) => {
      const request = requests[index]?.request;
      if (!request) throw new Error('No posted intent');
      const action =
        request.intent.case === 'pauseNow' ? IntentAction.PAUSE_NOW : IntentAction.PAUSE;
      const {records} = intentReplay(result, request.requestId, ++sequence, action);
      if (result === 'applied') {
        records.push(
          create(RecordSchema, {
            seq: ++sequence,
            epoch: 1,
            at: {seconds: 1700000101n},
            body: {case: 'lifecycle', value: {state: appliedState(action)}},
          }),
        );
      }
      return tailOf(records);
    },
  };
}

export interface RefusedResumeWorld {
  readonly fixture: StoreRunFixture;
  readonly requests: {projectRoot: string; runId: string}[];
  readonly postIntent: (host: string, bytes: number[]) => number[];
}

/** A finished run whose log records no project root, and a host that refuses to resume it (checkout missing). */
export function refusedResumeWorld(): RefusedResumeWorld {
  const source = storeRunFixture('short');
  const fixture = {
    ...source,
    records: source.records.map(record => {
      const copy = clone(RecordSchema, record);
      if (copy.body.case === 'epochStarted') copy.body.value.projectRoot = undefined;
      return copy;
    }),
  };
  const requests: {projectRoot: string; runId: string}[] = [];
  return {
    fixture,
    requests,
    postIntent: (_host, bytes) => {
      const envelope = fromBinary(HostCallRequestSchema, new Uint8Array(bytes));
      if (envelope.method.case !== 'relaunch') throw new Error('Expected Resume');
      requests.push(envelope.method.value);
      return [
        ...toBinary(
          HostCallResponseSchema,
          create(HostCallResponseSchema, {
            result: {
              case: 'relaunch',
              value: {outcome: {case: 'refusal', value: RelaunchRefusal.CHECKOUT_MISSING}},
            },
          }),
        ),
      ];
    },
  };
}
