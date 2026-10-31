import {clone, create} from '@bufbuild/protobuf';
import {
  EpochVersionSchema,
  type Keyframe,
  KeyframeSchema,
  LifecycleVersionSchema,
  type MetricHistory,
  MetricHistorySchema,
  MetricVersionSchema,
  Severity,
  type SpanHistory,
  SpanHistorySchema,
  SpanVersionSchema,
  type Record as StoreRecord,
  StreamKind,
} from '@vibesys/proto';
import {validateKeyframe} from './keyframe.js';
import {isRecordFact} from './record-facts.js';

/** Writer-only byte offsets do not belong to the client's data model. */
export type Skeleton = Pick<
  Keyframe,
  | 'spans'
  | 'metrics'
  | 'streams'
  | 'epochs'
  | 'lifecycles'
  | 'failures'
  | 'failuresTracked'
  | 'intents'
  | 'recordsTracked'
  | 'recordFacts'
  | 'head'
> & {
  readonly throughSeq: bigint | undefined;
};

/** Seed an owned skeleton from the generated store contract. Inputs are never mutated. */
export function seedSkeleton(keyframe?: Keyframe): Skeleton {
  if (keyframe) validateKeyframe(keyframe);
  return keyframe
    ? copySkeleton(keyframe)
    : {
        ...copySkeleton(create(KeyframeSchema, {failuresTracked: true, recordsTracked: true})),
        throughSeq: undefined,
      };
}

function copySkeleton(keyframe: Keyframe): Skeleton {
  const {
    throughSeq,
    spans,
    metrics,
    streams,
    epochs,
    lifecycles,
    failures,
    failuresTracked,
    intents,
    recordsTracked,
    recordFacts,
    head,
  } = clone(KeyframeSchema, keyframe);
  return {
    throughSeq,
    spans,
    metrics,
    streams,
    epochs,
    lifecycles,
    failures,
    failuresTracked,
    intents,
    recordsTracked,
    recordFacts,
    head,
  };
}

/**
 * A batch's working copy of a skeleton. Each collection is copied once when the batch opens, and a
 * span or metric history on its first touch, so a batch costs its own records plus the state's
 * pointers, never a deep copy of the state. Everything else is shared with the state it started
 * from and with the records it read, which the fold never mutates.
 */
interface Batch {
  readonly next: {-readonly [K in keyof Skeleton]: Skeleton[K]};
  /** The histories this batch created or copied: the only ones it may append to. */
  readonly owned: Set<object>;
  /** Position of each series and stream in `next`, built on the first one the batch meets. */
  metricAt?: Map<string, number>;
  streamAt?: Map<string, number>;
}

function openBatch(state: Skeleton): Batch {
  return {
    next: {
      throughSeq: state.throughSeq,
      spans: {...state.spans},
      metrics: [...state.metrics],
      streams: [...state.streams],
      epochs: [...state.epochs],
      lifecycles: [...state.lifecycles],
      // Replaced, never appended to.
      failures: state.failures,
      failuresTracked: state.failuresTracked,
      intents: [...state.intents],
      recordsTracked: state.recordsTracked,
      recordFacts: [...state.recordFacts],
      head: state.head,
    },
    owned: new Set(),
  };
}

/**
 * Typed records are dense. Phase 1 epoch/legacy records may start at zero and have gaps,
 * as served by the gateway during the seven-day read window. Every cursor strictly increases.
 *
 * Linear in the batch plus the state's pointers: neither `state` nor `records` is mutated, a
 * state returned earlier never changes, and whatever a batch does not touch is the same object in
 * the state it returns (the structure is shared, so a returned state is read-only).
 */
export function foldSkeleton(state: Skeleton, records: readonly StoreRecord[]): Skeleton {
  const batch = openBatch(state);
  const {next} = batch;
  for (const record of records) {
    validateSequence(next.throughSeq, record);
    if (next.head !== undefined && (next.head.committedSeq ?? 0n) < record.seq)
      next.head = undefined;
    foldBody(batch, record);
    next.throughSeq = record.seq;
  }
  return next;
}

function appendRecordFact({next}: Batch, record: StoreRecord): void {
  if (record.body.case === 'intent') next.intents.push(record);
  else if (record.body.case !== 'epochStarted' && isRecordFact(record))
    next.recordFacts.push(record);
}

/** The span's history for appending: copied the first time this batch touches it. */
function spanHistory({next, owned}: Batch, id: string): SpanHistory {
  const known = Object.hasOwn(next.spans, id) ? next.spans[id] : undefined;
  if (known !== undefined && owned.has(known)) return known;
  const history = known ? {...known, versions: [...known.versions]} : create(SpanHistorySchema);
  owned.add(history);
  // Assignment would reach the `__proto__` setter for a span of that name; define the property.
  Object.defineProperty(next.spans, id, {
    value: history,
    enumerable: true,
    writable: true,
    configurable: true,
  });
  return history;
}

/** The series' history for appending: copied the first time this batch touches it. */
function metricHistory(batch: Batch, series: string): MetricHistory {
  const {next, owned} = batch;
  batch.metricAt ??= new Map(next.metrics.map((history, at) => [history.series, at]));
  const at = batch.metricAt.get(series);
  const known = at === undefined ? undefined : next.metrics[at];
  if (known !== undefined && owned.has(known)) return known;
  const history = known
    ? {...known, points: [...known.points]}
    : create(MetricHistorySchema, {series});
  owned.add(history);
  if (at === undefined || known === undefined) {
    batch.metricAt.set(series, next.metrics.length);
    next.metrics.push(history);
  } else next.metrics[at] = history;
  return history;
}

function foldBody(batch: Batch, record: StoreRecord): void {
  const {next: state} = batch;
  const {seq, body} = record;
  appendInputDeclaration(state, record);
  appendRecordFact(batch, record);
  switch (body.case) {
    case 'span': {
      const span = body.value.span;
      if (!span) throw new Error(`record ${seq}: span.span is required`);
      spanHistory(batch, span.id).versions.push(create(SpanVersionSchema, {seq, span}));
      break;
    }
    case 'metric':
      metricHistory(batch, body.value.series).points.push(
        create(MetricVersionSchema, {seq, point: body.value}),
      );
      break;
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
      // The latest error notice of each epoch stands for all of them, as the writer's keyframe has it.
      if (body.value.severity >= Severity.ERROR)
        state.failures = [...state.failures.filter(kept => kept.epoch !== record.epoch), record];
      break;
    case 'stream': {
      batch.streamAt ??= new Map(state.streams.map((stream, at) => [stream.id, at]));
      const at = batch.streamAt.get(body.value.id);
      if (at === undefined) {
        batch.streamAt.set(body.value.id, state.streams.length);
        state.streams.push(body.value);
      } else state.streams[at] = body.value;
      break;
    }
    default:
      break;
  }
}

/** Store upgrades carry a declaration alongside the original metric record. */
function appendInputDeclaration(state: Batch['next'], record: StoreRecord): void {
  if (record.inputMeasurement !== undefined) {
    const epoch = state.epochs.at(-1)?.epoch;
    if (epoch?.run) {
      const amended = clone(
        EpochVersionSchema,
        create(EpochVersionSchema, {seq: record.seq, epoch}),
      );
      if (amended.epoch?.run) amended.epoch.run.inputMeasurement = record.inputMeasurement;
      state.epochs.push(amended);
    }
  }
}

/** Upper bound in one history, without visiting the run's records. */
function boundary<T extends {seq: bigint}>(history: readonly T[], seq: bigint): number {
  let low = 0;
  let high = history.length;
  while (low < high) {
    const mid = Math.floor((low + high) / 2);
    const item = history[mid];
    if (item && item.seq <= seq) low = mid + 1;
    else high = mid;
  }
  return low;
}

/**
 * Lookup at s: binary search each version history, plus the cost of returned points/epochs.
 * No replay, even for a keyframe-seeded run. Streams are the current index: store.v1 carries
 * no stream sequence history. Writer log offsets are excluded from the model.
 */
export function seekSkeleton(state: Skeleton, seq: bigint) {
  if (seq < 0n || seq > (state.throughSeq ?? 0n)) throw new Error(`seek.seq: out of range ${seq}`);
  const spans = Object.fromEntries(
    Object.entries(state.spans).flatMap(([id, history]) => {
      const version = history.versions[boundary(history.versions, seq) - 1];
      return version?.span ? [[id, version.span]] : [];
    }),
  );
  return {
    seq,
    spans,
    metrics: state.metrics
      .map(history => ({
        series: history.series,
        points: history.points.slice(0, boundary(history.points, seq)),
      }))
      .filter(history => history.points.length > 0),
    epochs: state.epochs.slice(0, boundary(state.epochs, seq)),
    lifecycle: state.lifecycles[boundary(state.lifecycles, seq) - 1]?.lifecycle,
  };
}

function validateSequence(previous: bigint | undefined, record: StoreRecord): void {
  const expected = previous === undefined ? 1n : previous + 1n;
  const legacy =
    record.body.case === 'legacyEvent' ||
    (record.body.case === 'epochStarted' && !record.body.value.run);
  if (
    (legacy && (record.seq < 0n || (previous !== undefined && record.seq <= previous))) ||
    (!legacy && record.seq !== expected)
  ) {
    throw new Error(
      `record.seq: expected ${legacy ? 'an increasing phase 1 sequence' : expected}, got ${record.seq}`,
    );
  }
}

/**
 * Error notices read from the log, which a keyframe without failure facts could not give: the
 * latest of each epoch stands for all of them, and none past the prefix counts.
 */
export function absorbFailures(state: Skeleton, found: readonly StoreRecord[]): Skeleton {
  let failures = state.failures;
  for (const record of found) {
    if (record.body.case !== 'notice' || record.body.value.severity < Severity.ERROR) continue;
    if (record.seq > (state.throughSeq ?? 0n)) continue;
    const kept = failures.find(item => item.epoch === record.epoch);
    if (kept !== undefined && kept.seq >= record.seq) continue;
    failures = [...failures.filter(item => item.epoch !== record.epoch), record];
  }
  return {...state, failures};
}

/** Older checkpoints recover their omitted facts from the same progressive Tail as the raw view. */
export function absorbRecordFacts(
  state: Skeleton,
  records: readonly StoreRecord[],
  through: bigint,
): Skeleton {
  const historical = records.filter(record => record.seq <= (state.throughSeq ?? 0n));
  if (historical.length === 0) return state;
  const next = absorbFailures(state, historical);
  const intents = new Map(next.intents.map(record => [record.seq, record]));
  const facts = new Map(next.recordFacts.map(record => [record.seq, record]));
  const starts = new Map(
    historical
      .filter(record => record.body.case === 'epochStarted')
      .map(record => [record.seq, record]),
  );
  for (const record of historical) {
    if (record.body.case === 'intent') intents.set(record.seq, record);
    else if (record.body.case !== 'epochStarted' && isRecordFact(record))
      facts.set(record.seq, record);
  }
  return {
    ...next,
    intents: [...intents.values()].sort(bySequence),
    recordFacts: [...facts.values()].sort(bySequence),
    epochs: next.epochs.map(version => {
      const start = starts.get(version.seq);
      return start ? {...version, startsEpoch: true, startedAt: start.at} : version;
    }),
    recordsTracked: state.recordsTracked || through >= (state.throughSeq ?? 0n),
    failuresTracked: state.failuresTracked || through >= (state.throughSeq ?? 0n),
  };
}

function bySequence(a: {seq: bigint}, b: {seq: bigint}): number {
  return a.seq < b.seq ? -1 : a.seq > b.seq ? 1 : 0;
}

/** The transcript stream the run recorded for this span, if it has produced content. */
export function transcriptStreamFor(
  skeleton: Skeleton | undefined,
  span: string | null,
): string | null {
  if (span === null) return null;
  return (
    skeleton?.streams.find(stream => stream.kind === StreamKind.TRANSCRIPT && stream.span === span)
      ?.id ?? null
  );
}
