import {clone, create} from '@bufbuild/protobuf';
import {
  EpochVersionSchema,
  type Keyframe,
  KeyframeSchema,
  LifecycleVersionSchema,
  MetricHistorySchema,
  MetricVersionSchema,
  RecordSchema,
  Severity,
  SpanHistorySchema,
  SpanVersionSchema,
  type Record as StoreRecord,
  StreamKind,
} from '@vibesys/proto';
import {validateKeyframe} from './keyframe.js';

/** Writer-only byte offsets do not belong to the client's data model. */
export type Skeleton = Pick<
  Keyframe,
  'spans' | 'metrics' | 'streams' | 'epochs' | 'lifecycles' | 'failures' | 'failuresTracked'
> & {
  readonly throughSeq: bigint | undefined;
};

/** Seed an owned skeleton from the generated store contract. Inputs are never mutated. */
export function seedSkeleton(keyframe?: Keyframe): Skeleton {
  if (keyframe) validateKeyframe(keyframe);
  return keyframe
    ? copySkeleton(keyframe)
    : {...copySkeleton(create(KeyframeSchema, {failuresTracked: true})), throughSeq: undefined};
}

function copySkeleton(keyframe: Keyframe): Skeleton {
  const {throughSeq, spans, metrics, streams, epochs, lifecycles, failures, failuresTracked} =
    clone(KeyframeSchema, keyframe);
  return {throughSeq, spans, metrics, streams, epochs, lifecycles, failures, failuresTracked};
}

/**
 * Typed records are dense. Phase 1 epoch/legacy records may start at zero and have gaps,
 * as served by the gateway during the seven-day read window. Every cursor strictly increases.
 */
export function foldSkeleton(state: Skeleton, records: readonly StoreRecord[]): Skeleton {
  const next = {
    ...copySkeleton(create(KeyframeSchema, {...state, throughSeq: state.throughSeq ?? 0n})),
    throughSeq: state.throughSeq,
  };
  for (const record of records) {
    validateSequence(next.throughSeq, record);
    foldBody(next, clone(RecordSchema, record));
    next.throughSeq = record.seq;
  }
  return next;
}

function foldBody(state: Omit<Skeleton, 'throughSeq'>, record: StoreRecord): void {
  const {seq, body} = record;
  appendInputDeclaration(state, record);
  switch (body.case) {
    case 'span': {
      const span = body.value.span;
      if (!span) throw new Error(`record ${seq}: span.span is required`);
      const history =
        (Object.hasOwn(state.spans, span.id) ? state.spans[span.id] : undefined) ??
        create(SpanHistorySchema);
      history.versions.push(create(SpanVersionSchema, {seq, span}));
      state.spans = {...state.spans, [span.id]: history};
      break;
    }
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
      state.epochs.push(create(EpochVersionSchema, {seq, epoch: body.value}));
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
      const index = state.streams.findIndex(stream => stream.id === body.value.id);
      if (index < 0) state.streams.push(body.value);
      else state.streams[index] = body.value;
      break;
    }
    default:
      break;
  }
}

/** Store upgrades carry a declaration alongside the original metric record. */
function appendInputDeclaration(state: Omit<Skeleton, 'throughSeq'>, record: StoreRecord): void {
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
