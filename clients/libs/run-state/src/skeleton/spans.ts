/** Store spans become the screen's existing span value, without replaying legacy events. */
import {type FileRegistry, fromBinary, toJson} from '@bufbuild/protobuf';
import type {Span} from '@vibesys/contracts';
import {
  AttemptOutcome,
  descriptorRegistry,
  type EpochStarted,
  RuntimeType,
  SpanFailureKind,
  SpanStatus,
  type Span as StoreSpan,
  WaitKind,
} from '@vibesys/proto';
import type {StoreHistory} from './types.js';

const STATUSES: Partial<Record<SpanStatus, Span['status']>> = {
  [SpanStatus.RUNNING]: 'running',
  [SpanStatus.WAITING]: 'waiting',
  [SpanStatus.PENDING]: 'pending',
  [SpanStatus.DONE]: 'done',
  [SpanStatus.FAILED]: 'failed',
  [SpanStatus.STOPPED]: 'stopped',
};
const WAITS: Partial<Record<WaitKind, NonNullable<Span['waiting_on']>[number]['kind']>> = {
  [WaitKind.OPERATION]: 'operation',
  [WaitKind.SLOT]: 'slot',
  [WaitKind.TIME]: 'time',
  [WaitKind.STOP]: 'stop',
};

function attrs(
  span: StoreSpan,
  epoch: EpochStarted | undefined,
  registries: Map<EpochStarted, FileRegistry>,
): string {
  const label = span.label;
  if (!label?.attrs) throw new Error(`span ${span.id}: attrs required`);
  const declaration = epoch?.spanKinds.find(kind => kind.kind === label.kind);
  const name = label.attrs.typeUrl.split('/').at(-1);
  if (!declaration || declaration.attrsType !== name || !epoch?.descriptors)
    throw new Error(`span ${span.id}: undeclared attrs type`);
  let registry = registries.get(epoch);
  if (!registry) {
    registry = descriptorRegistry(epoch.descriptors);
    registries.set(epoch, registry);
  }
  const schema = registry.getMessage(declaration.attrsType);
  if (!schema) throw new Error(`span ${span.id}: missing attrs descriptor`);
  return JSON.stringify(
    toJson(schema, fromBinary(schema, label.attrs.value), {useProtoFieldName: true}),
  );
}

function screenSpan(
  span: StoreSpan,
  skeleton: StoreHistory,
  registries: Map<EpochStarted, FileRegistry>,
): Span {
  const start = skeleton.spans[span.id]?.versions[0]?.seq ?? 0n;
  const epoch = skeleton.epochs.filter(item => item.seq <= start).at(-1)?.epoch;
  const status = STATUSES[span.status];
  if (!status) throw new Error(`span ${span.id}: invalid status`);
  return {
    id: span.id,
    parent: span.parent ?? null,
    type: known(
      ({[RuntimeType.FLOW]: 'flow', [RuntimeType.OPERATION]: 'operation'} as const)[
        span.runtimeType as RuntimeType.FLOW | RuntimeType.OPERATION
      ],
      `span ${span.id}: runtime_type`,
    ),
    name: span.name,
    status,
    started_at: span.startedAt,
    ended_at: span.endedAt ?? null,
    waiting_on: span.waitingOn.map(wait => {
      const kind = WAITS[wait.kind];
      if (!kind) throw new Error(`span ${span.id}: invalid wait`);
      return {kind, ref: wait.ref, since: wait.since};
    }),
    attempts: span.attempts.map(attempt => ({
      n: attempt.n,
      of: attempt.of,
      started_at: attempt.startedAt,
      ended_at: attempt.endedAt ?? null,
      outcome:
        attempt.outcome === undefined
          ? null
          : known(
              (
                {
                  [AttemptOutcome.DONE]: 'done',
                  [AttemptOutcome.AGAIN]: 'again',
                  [AttemptOutcome.GAVE_UP]: 'gave_up',
                } as const
              )[
                attempt.outcome as
                  | AttemptOutcome.DONE
                  | AttemptOutcome.AGAIN
                  | AttemptOutcome.GAVE_UP
              ],
              `span ${span.id}: attempt.outcome`,
            ),
      reason: attempt.reason ?? null,
    })),
    failure: span.failure
      ? {
          kind: known(
            (
              {
                [SpanFailureKind.OUTCOME]: 'outcome',
                [SpanFailureKind.INTERRUPTION]: 'interruption',
              } as const
            )[span.failure.kind as SpanFailureKind.OUTCOME | SpanFailureKind.INTERRUPTION],
            `span ${span.id}: failure.kind`,
          ),
          message: span.failure.message,
        }
      : null,
    stop: span.stop ? {requested_at: span.stop.requestedAt, landed: span.stop.landed} : null,
    label: span.label
      ? {
          kind: span.label.kind,
          title: span.label.title,
          verdict: span.label.verdict ?? null,
          reason: span.label.reason ?? null,
          links: span.label.links.map(link => ({rel: link.rel, to: link.to})),
          attrs_json: attrs(span, epoch, registries),
        }
      : null,
  };
}

export function screenSpans(spans: readonly StoreSpan[], skeleton: StoreHistory): Span[] {
  const registries = new Map<EpochStarted, FileRegistry>();
  return spans.map(span => screenSpan(span, skeleton, registries));
}

function known<T>(value: T | undefined, path: string): T {
  if (value === undefined) throw new Error(`${path}: invalid enum`);
  return value;
}
