/**
 * The fold over any batching of a run's records. Input space: streams of 0 to 48 records of every
 * body kind (spans and series that collide with `Object` names, steer links, intents, blobs,
 * chat, notices of three epochs, epochs with and without a run and the input measurement that
 * amends them, streams that are re-sent), cut into batches anywhere.
 */
import {expect, test} from 'bun:test';
import {clone, create, fromBinary, type MessageInitShape, toBinary} from '@bufbuild/protobuf';
import {
  Change,
  Direction,
  EpochVersionSchema,
  IntentAction,
  IntentStep,
  KeyframeSchema,
  LifecycleVersionSchema,
  MetricHistorySchema,
  MetricVersionSchema,
  RecordSchema,
  RuntimeType,
  Severity,
  SpanHistorySchema,
  SpanStatus,
  SpanVersionSchema,
  type Record as StoreRecord,
  StreamKind,
  StreamState,
} from '@vibesys/proto';
import fc from 'fast-check';
import {foldSkeleton, type Skeleton, seedSkeleton} from './index.js';
import {isRecordFact} from './record-facts.js';

type Draft = {-readonly [K in keyof Skeleton]: Skeleton[K]};

/**
 * The fold as it was before it became linear, kept whole as the specification the linear one must
 * equal: it deep-copies the state and every record, and spreads every span for each span record.
 * Streams here are dense by construction, so it needs no sequence check.
 */
function referenceFold(state: Skeleton, records: readonly StoreRecord[]): Skeleton {
  const copy = clone(
    KeyframeSchema,
    create(KeyframeSchema, {...state, throughSeq: state.throughSeq ?? 0n}),
  );
  const next: Draft = {
    throughSeq: state.throughSeq,
    spans: copy.spans,
    metrics: copy.metrics,
    streams: copy.streams,
    epochs: copy.epochs,
    lifecycles: copy.lifecycles,
    failures: copy.failures,
    failuresTracked: copy.failuresTracked,
    intents: copy.intents,
    recordsTracked: copy.recordsTracked,
    recordFacts: copy.recordFacts,
    head: copy.head,
  };
  for (const record of records) {
    if (next.head !== undefined && (next.head.committedSeq ?? 0n) < record.seq)
      next.head = undefined;
    referenceBody(next, clone(RecordSchema, record));
    next.throughSeq = record.seq;
  }
  return next;
}

function referenceBody(state: Draft, record: StoreRecord): void {
  const {seq, body} = record;
  referenceDeclaration(state, record);
  if (body.case === 'intent') state.intents.push(record);
  else if (body.case !== 'epochStarted' && isRecordFact(record)) state.recordFacts.push(record);
  switch (body.case) {
    case 'span':
      referenceSpan(state, record);
      break;
    case 'metric': {
      let history = state.metrics.find(item => item.series === body.value.series);
      if (!history) {
        history = create(MetricHistorySchema, {series: body.value.series});
        state.metrics.push(history);
      }
      history.points.push(create(MetricVersionSchema, {seq, point: body.value}));
      break;
    }
    case 'epochStarted':
    case 'declarationAmended':
      state.epochs.push(
        create(EpochVersionSchema, {
          seq,
          epoch: body.value,
          startsEpoch: body.case === 'epochStarted',
          startedAt: body.case === 'epochStarted' ? record.at : undefined,
        }),
      );
      break;
    case 'lifecycle':
      state.lifecycles.push(create(LifecycleVersionSchema, {seq, lifecycle: body.value}));
      break;
    case 'notice':
      if (body.value.severity >= Severity.ERROR)
        state.failures = [...state.failures.filter(kept => kept.epoch !== record.epoch), record];
      break;
    case 'stream': {
      const index = state.streams.findIndex(stream => stream.id === body.value.id);
      if (index < 0) state.streams.push(body.value);
      else state.streams[index] = body.value;
      break;
    }
    default:
      break;
  }
}

function referenceDeclaration(state: Draft, record: StoreRecord): void {
  const epoch = state.epochs.at(-1)?.epoch;
  if (record.inputMeasurement === undefined || !epoch?.run) return;
  const amended = clone(EpochVersionSchema, create(EpochVersionSchema, {seq: record.seq, epoch}));
  if (amended.epoch?.run) amended.epoch.run.inputMeasurement = record.inputMeasurement;
  state.epochs.push(amended);
}

function referenceSpan(state: Draft, record: StoreRecord): void {
  const span = record.body.case === 'span' ? record.body.value.span : undefined;
  if (!span) throw new Error(`record ${record.seq}: span.span is required`);
  const history =
    (Object.hasOwn(state.spans, span.id) ? state.spans[span.id] : undefined) ??
    create(SpanHistorySchema);
  history.versions.push(create(SpanVersionSchema, {seq: record.seq, span}));
  state.spans = {...state.spans, [span.id]: history};
}

type Body = NonNullable<MessageInitShape<typeof RecordSchema>['body']>;
interface Spec {
  readonly body: Body;
  readonly epoch: number;
  /** The record carries the input measurement a store upgrade supplies beside a metric. */
  readonly input: boolean;
}

// Names that an object keyed by id would mishandle: its own prototype slot and inherited members.
const IDS = ['a', 'b', 'c', '__proto__', 'constructor', 'toString'] as const;
const SERIES = ['cost', 'score', '__proto__'] as const;
const epoch = fc.integer({min: 1, max: 3});
const run = {
  strategy: 'search',
  input: 'input',
  objectives: [{series: 'cost', direction: Direction.MIN}],
  expectedRoles: [],
};

const specs: fc.Arbitrary<Spec> = fc
  .oneof(
    fc
      .record({
        id: fc.constantFrom(...IDS),
        status: fc.constantFrom(SpanStatus.RUNNING, SpanStatus.WAITING, SpanStatus.DONE),
        steer: fc.boolean(),
      })
      .map(({id, status, steer}) => ({
        body: {
          case: 'span',
          value: {
            change: Change.UPDATED,
            span: {
              id,
              name: `span-${id}`,
              runtimeType: RuntimeType.OPERATION,
              status,
              startedAt: 1,
              label: steer
                ? {kind: 'turn', title: 'turn', links: [{rel: 'steer', to: 'z'}]}
                : undefined,
            },
          },
        } satisfies Body,
      })),
    fc
      .record({series: fc.constantFrom(...SERIES), value: fc.integer({min: -9, max: 9})})
      .map(({series, value}) => ({
        body: {case: 'metric', value: {series, value, clock: 1}} satisfies Body,
      })),
    fc.constantFrom('running', 'parked').map(state => ({
      body: {case: 'lifecycle', value: {state: {case: state, value: {}}}} satisfies Body,
    })),
    fc
      .record({
        kind: fc.constantFrom('epochStarted', 'declarationAmended'),
        number: epoch,
        withRun: fc.boolean(),
      })
      .map(({kind, number, withRun}) => ({
        body: {
          case: kind,
          value: {runId: 'run-a', epoch: number, ...(withRun ? {run} : {})},
        } satisfies Body,
      })),
    fc.constantFrom(Severity.ERROR, Severity.WARNING).map(severity => ({
      body: {case: 'notice', value: {severity, message: 'm'}} satisfies Body,
    })),
    fc.constantFrom('log-a', 'log-b').map(id => ({
      body: {
        case: 'stream',
        value: {id, kind: StreamKind.LOG, title: id, state: StreamState.OPENED},
      } satisfies Body,
    })),
    fc
      .constantFrom(
        {
          case: 'intent',
          value: {controlId: 'c', action: IntentAction.STEER, step: IntentStep.APPLIED},
        },
        {case: 'blob', value: {span: 'a', role: 'reply'}},
        {case: 'chat', value: {}},
        {case: 'legacyEvent', value: {json: '{}'}},
        {case: undefined},
      )
      .map(body => ({body: body as Body})),
  )
  .chain(spec =>
    fc
      .record({epoch, input: fc.boolean()})
      .map(extra => ({...spec, epoch: extra.epoch, input: extra.input})),
  );

/** Records as the loader holds them: dense from 1, and decoded from their wire bytes. */
function recordsOf(streamSpecs: readonly Spec[]): StoreRecord[] {
  return streamSpecs.map(({body, epoch: number, input}, index) =>
    fromBinary(
      RecordSchema,
      toBinary(
        RecordSchema,
        create(RecordSchema, {
          seq: BigInt(index + 1),
          epoch: number,
          at: {seconds: BigInt(index)},
          body,
          ...(input
            ? {
                inputMeasurement: {
                  state: {case: 'measured', value: {series: 'cost', value: 1, span: 'a'}},
                },
              }
            : {}),
        }),
      ),
    ),
  );
}

const streams = fc.array(specs, {maxLength: 48}).map(recordsOf);
const cuts = fc.array(fc.nat(), {maxLength: 6});

/** The batches the cut points make: any cut, in any order, repeated or past the end. */
function batched<T>(items: readonly T[], points: readonly number[]): T[][] {
  const at = [...new Set(points.map(point => point % (items.length + 1)))].sort((a, b) => a - b);
  const edges = [0, ...at, items.length];
  return edges.slice(1).map((end, index) => items.slice(edges[index], end));
}

function bytesOf(state: Skeleton): Uint8Array {
  return toBinary(
    KeyframeSchema,
    create(KeyframeSchema, {...state, throughSeq: state.throughSeq ?? 0n}),
  );
}

/** Freeze the whole graph, so that a write to it throws instead of passing unnoticed. */
function deepFreeze<T>(value: T): T {
  if (typeof value !== 'object' || value === null || Object.isFrozen(value)) return value;
  // A typed array with elements cannot be frozen, and the fold never writes into one.
  if (!ArrayBuffer.isView(value)) {
    Object.freeze(value);
    for (const child of Object.values(value)) deepFreeze(child);
  }
  return value;
}

const OPTIONS = {numRuns: 150, seed: 20261030};

test('the linear fold equals the reference fold after every batch, under any batching', () => {
  fc.assert(
    fc.property(streams, cuts, (records, points) => {
      let linear = seedSkeleton();
      let reference = seedSkeleton();
      for (const batch of batched(records, points)) {
        linear = foldSkeleton(linear, batch);
        reference = referenceFold(reference, batch);
        expect(linear).toEqual(reference);
        expect(bytesOf(linear)).toEqual(bytesOf(reference));
      }
      expect(linear).toEqual(foldSkeleton(seedSkeleton(), records));
    }),
    OPTIONS,
  );
});

test('a fold never writes to its inputs or to a state returned earlier', () => {
  fc.assert(
    fc.property(streams, cuts, (records, points) => {
      deepFreeze(records);
      let state = deepFreeze(seedSkeleton());
      const returned: {state: Skeleton; bytes: Uint8Array}[] = [];
      for (const batch of batched(records, points)) {
        const next = deepFreeze(foldSkeleton(state, batch));
        // A batch that fails after folding some records leaves the state it started from as it was.
        const gap = create(RecordSchema, {
          seq: (state.throughSeq ?? 0n) + 2n + BigInt(batch.length),
        });
        expect(() => foldSkeleton(state, [...batch, gap])).toThrow('record.seq');
        returned.push({state: next, bytes: bytesOf(next)});
        state = next;
      }
      for (const {state: earlier, bytes} of returned) expect(bytesOf(earlier)).toEqual(bytes);
    }),
    OPTIONS,
  );
});

test('what a batch does not touch is the same object in the state it returns', () => {
  fc.assert(
    fc.property(streams, fc.nat(), (records, point) => {
      const [first = [], second = []] = batched(records, [point]);
      const before = foldSkeleton(seedSkeleton(), first);
      const after = foldSkeleton(before, second);
      const spanIds = new Set(
        second.flatMap(record => (record.body.case === 'span' ? [record.body.value.span?.id] : [])),
      );
      const seriesNames = new Set(
        second.flatMap(record => (record.body.case === 'metric' ? [record.body.value.series] : [])),
      );
      for (const [id, history] of Object.entries(before.spans)) {
        const folded = after.spans[id];
        if (spanIds.has(id)) {
          expect(folded).not.toBe(history);
          // The copy keeps what the history already held, version for version.
          history.versions.forEach((version, index) => {
            expect(folded?.versions[index]).toBe(version);
          });
        } else expect(folded).toBe(history);
      }
      for (const history of before.metrics) {
        const folded = after.metrics.find(item => item.series === history.series);
        if (seriesNames.has(history.series)) expect(folded).not.toBe(history);
        else expect(folded).toBe(history);
      }
      for (const kind of ['epochs', 'lifecycles', 'intents', 'recordFacts'] as const)
        before[kind].forEach((item, index) => {
          expect(after[kind][index]).toBe(item);
        });
    }),
    OPTIONS,
  );
});
