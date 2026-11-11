import {
  Change,
  Direction,
  descriptorRegistry,
  type EpochStarted,
  type Keyframe,
  Outcome,
  RecordSchema,
  SeriesRole,
  type SkeletonResponse,
  type Span,
  type Record as StoreRecord,
  type TailResponse,
  validatedAddressedKeyframe,
  validatedFromBinary,
  verifiedDescriptorMessages,
} from '@vibesys/proto';
import {projectTypedSpan} from './span-projectors.js';
import {
  type AgentState,
  type AttributeIssue,
  type CorrectnessGateState,
  DEFAULT_RUN_DETAIL_LIMITS,
  type DescriptorKnowledge,
  type EpochKnowledge,
  type LifecycleProjection,
  type MetricDirection,
  type MetricRole,
  type MetricSeriesState,
  type RunDetailCursor,
  type RunDetailLimits,
  type RunDetailState,
  type RunDetailUpdate,
  type WorkstreamState,
} from './types.js';

const DIRECTIONS: Partial<Record<Direction, MetricDirection>> = {
  [Direction.MAX]: 'max',
  [Direction.MIN]: 'min',
  [Direction.NONE]: 'none',
};
const ROLES: Partial<Record<SeriesRole, MetricRole>> = {
  [SeriesRole.OBJECTIVE]: 'objective',
  [SeriesRole.RESOURCE]: 'resource',
  [SeriesRole.REFERENCE]: 'reference',
};

const EVIDENCE_LIMITS: RunDetailLimits = {
  maxAgents: Number.MAX_SAFE_INTEGER,
  maxCorrectnessGates: Number.MAX_SAFE_INTEGER,
  maxDescriptorSets: Number.MAX_SAFE_INTEGER,
  maxEpochVersions: Number.MAX_SAFE_INTEGER,
  maxIssues: Number.MAX_SAFE_INTEGER,
  maxMetricPointsPerSeries: Number.MAX_SAFE_INTEGER,
  maxMetricSeries: Number.MAX_SAFE_INTEGER,
  maxWorkstreams: Number.MAX_SAFE_INTEGER,
};

class HistoryEvictedError extends Error {}

export function emptyRunDetail(
  runId: string,
  limits: Partial<RunDetailLimits> = {},
): RunDetailState {
  if (!/^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(runId)) throw new Error('runId: invalid');
  const resolved = {...DEFAULT_RUN_DETAIL_LIMITS, ...limits};
  for (const [name, value] of Object.entries(resolved)) {
    if (!Number.isSafeInteger(value) || value <= 0)
      throw new Error(`limits.${name}: must be positive`);
  }
  return {
    runId,
    limits: resolved,
    cursor: {kind: 'empty'},
    throughSequence: undefined,
    descriptors: [],
    epochs: [],
    agents: [],
    workstreams: [],
    correctnessGates: [],
    metrics: [],
    lifecycle: undefined,
    issues: [],
    undeclaredMetricPoints: 0,
    truncation: {
      agentEvictions: 0,
      correctnessGateEvictions: 0,
      descriptorSetEvictions: 0,
      epochEvictions: 0,
      issueEvictions: 0,
      metricSeriesEvictions: 0,
      workstreamEvictions: 0,
    },
  };
}

/** Replace all projected facts with one gateway-frozen committed prefix. */
export function ingestRunDetailSkeleton(
  previous: RunDetailState,
  response: SkeletonResponse,
): RunDetailUpdate {
  try {
    const restored = restoreDescriptors(previous, response);
    let state = {...restored.state, limits: EVIDENCE_LIMITS};
    const keyframe = decodeKeyframe(restored.available, response);
    if (keyframe) state = foldKeyframe(state, keyframe, restored.available);
    const records = response.records.map(bytes => validatedFromBinary(RecordSchema, bytes));
    const sequencePolicy = response.head === undefined ? 'lower-bound' : 'contiguous';
    validateSnapshotRecords(keyframe?.throughSeq, records, sequencePolicy);
    for (const record of records) state = foldRecord(state, record);
    const includedThrough = records.at(-1)?.seq ?? keyframe?.throughSeq;
    validateSnapshotBoundary(response, includedThrough);
    state = finalizeEvidence(state, previous.limits);
    state = finishSnapshot(state, response, includedThrough);
    return {kind: 'applied', state};
  } catch (error) {
    return {kind: 'rejected-snapshot', state: previous, reason: errorMessage(error)};
  }
}

/** Apply one ordered Tail response. Replays below the cursor are idempotent. */
export function ingestRunDetailTail(
  previous: RunDetailState,
  response: TailResponse,
): RunDetailUpdate {
  if (previous.cursor.kind === 'refresh-required') {
    return {kind: 'refresh-required', state: previous, reason: previous.cursor.reason};
  }
  if (previous.cursor.kind === 'empty') {
    return refresh(previous, 0n, response.throughSeq, 'invalid-record');
  }
  const sequencePolicy = response.head === undefined ? 'lower-bound' : 'contiguous';
  if (sequencePolicy !== previous.cursor.sequencePolicy) {
    return refresh(previous, previous.cursor.nextSequence, response.throughSeq, 'invalid-record');
  }
  const read = readTail(response);
  if (read.kind === 'invalid') {
    return refresh(previous, previous.cursor.nextSequence, read.observedSequence, read.reason);
  }
  const selected = selectTailRecords(read.records, previous.cursor.nextSequence, sequencePolicy);
  if (selected.kind === 'gap') {
    return refresh(previous, selected.expectedSequence, selected.observedSequence, 'gap');
  }
  if (selected.records.length === 0) return {kind: 'duplicate', state: previous};

  try {
    let state = previous;
    for (const record of selected.records) state = foldRecord(state, record);
    const throughSequence = selected.records.at(-1)?.seq;
    if (throughSequence === undefined) return {kind: 'duplicate', state: previous};
    state = {
      ...state,
      throughSequence,
      cursor: {
        kind: 'ready',
        nextSequence: selected.nextSequence,
        committedSequence: previous.cursor.committedSequence,
        complete:
          previous.cursor.complete ||
          (sequencePolicy === 'contiguous' && throughSequence >= previous.cursor.committedSequence),
        sequencePolicy,
      },
    };
    return {kind: 'applied', state};
  } catch (error) {
    return refresh(
      previous,
      previous.cursor.nextSequence,
      read.records[0]?.seq ?? 0n,
      error instanceof HistoryEvictedError ? 'history-evicted' : 'invalid-record',
    );
  }
}

interface RestoredDescriptors {
  readonly state: RunDetailState;
  /** Complete only for this Skeleton decode; retained descriptor knowledge stays bounded. */
  readonly available: ReadonlyMap<string, DescriptorKnowledge>;
}

function restoreDescriptors(
  previous: RunDetailState,
  response: SkeletonResponse,
): RestoredDescriptors {
  let state = emptyRunDetail(previous.runId, previous.limits);
  const available = new Map<string, DescriptorKnowledge>();
  for (const known of previous.descriptors) {
    available.set(known.sha256, known);
    state = addDescriptor(state, known);
  }
  const responseHashes = new Set<string>();
  const descriptorSets = [...response.descriptorSets].sort((left, right) =>
    left.sha256.localeCompare(right.sha256),
  );
  for (const descriptorSet of descriptorSets) {
    if (responseHashes.has(descriptorSet.sha256)) {
      throw new Error(`descriptor_sets: duplicate ${descriptorSet.sha256}`);
    }
    responseHashes.add(descriptorSet.sha256);
    const data = Uint8Array.from(descriptorSet.data);
    const knowledge = {
      sha256: descriptorSet.sha256,
      data,
      messageTypes: verifiedDescriptorMessages(descriptorSet.sha256, data).map(
        descriptor => descriptor.typeName,
      ),
    };
    available.set(descriptorSet.sha256, knowledge);
    state = addDescriptor(state, knowledge);
  }
  return {state, available};
}

function decodeKeyframe(
  availableDescriptors: ReadonlyMap<string, DescriptorKnowledge>,
  response: SkeletonResponse,
): Keyframe | undefined {
  if (response.keyframe === undefined) return undefined;
  const keyframe = validatedAddressedKeyframe(
    response.keyframe,
    new Map(
      [...availableDescriptors].map(([sha256, descriptor]) => [sha256, descriptor.data] as const),
    ),
  );
  validateKeyframe(keyframe);
  if (keyframe.throughSeq > response.committedSeq) {
    throw new Error('keyframe.through_seq: exceeds committed_seq');
  }
  return keyframe;
}

function validateSnapshotBoundary(
  response: SkeletonResponse,
  includedThrough: bigint | undefined,
): void {
  const logicalThrough = includedThrough ?? 0n;
  if (logicalThrough > response.committedSeq) {
    throw new Error('skeleton.records: exceeds committed_seq');
  }
  if (response.head !== undefined && response.head.committedSeq !== response.committedSeq) {
    throw new Error('skeleton.head.committed_seq: does not match committed_seq');
  }
  const headedEmptySentinel =
    response.head !== undefined &&
    logicalThrough === 0n &&
    response.committedSeq === 0n &&
    response.nextSeq === 0n;
  if (
    response.head !== undefined &&
    !headedEmptySentinel &&
    response.nextSeq !== logicalThrough + 1n
  ) {
    throw new Error('skeleton.next_seq: does not follow contiguous delivered prefix');
  }
  if (
    response.head === undefined &&
    includedThrough !== undefined &&
    response.nextSeq <= includedThrough
  ) {
    throw new Error('skeleton.next_seq: does not follow delivered prefix');
  }
  if (response.complete && logicalThrough !== response.committedSeq) {
    throw new Error('skeleton.complete: inconsistent committed prefix');
  }
}

function finishSnapshot(
  state: RunDetailState,
  response: SkeletonResponse,
  includedThrough: bigint | undefined,
): RunDetailState {
  return {
    ...state,
    throughSequence: includedThrough,
    cursor: {
      kind: 'ready',
      nextSequence:
        response.head !== undefined && includedThrough === undefined && response.nextSeq === 0n
          ? 1n
          : response.nextSeq,
      committedSequence: response.committedSeq,
      complete: response.complete,
      sequencePolicy: response.head === undefined ? 'lower-bound' : 'contiguous',
    },
  };
}

type TailRead =
  | {readonly kind: 'valid'; readonly records: readonly StoreRecord[]}
  | {
      readonly kind: 'invalid';
      readonly reason: 'invalid-order' | 'invalid-record' | 'invalid-through-sequence';
      readonly observedSequence: bigint;
    };

function readTail(response: TailResponse): TailRead {
  let records: readonly StoreRecord[];
  try {
    records = response.records.map(bytes => validatedFromBinary(RecordSchema, bytes));
  } catch {
    return {kind: 'invalid', reason: 'invalid-record', observedSequence: response.throughSeq};
  }
  const first = records[0];
  const last = records.at(-1);
  if (!first || !last) {
    return {kind: 'invalid', reason: 'invalid-record', observedSequence: response.throughSeq};
  }
  for (let index = 1; index < records.length; index += 1) {
    const before = records[index - 1];
    const after = records[index];
    if (!before || !after || after.seq <= before.seq) {
      return {kind: 'invalid', reason: 'invalid-order', observedSequence: after?.seq ?? first.seq};
    }
  }
  return last.seq === response.throughSeq
    ? {kind: 'valid', records}
    : {
        kind: 'invalid',
        reason: 'invalid-through-sequence',
        observedSequence: response.throughSeq,
      };
}

type TailSelection =
  | {
      readonly kind: 'selected';
      readonly records: readonly StoreRecord[];
      readonly nextSequence: bigint;
    }
  | {readonly kind: 'gap'; readonly expectedSequence: bigint; readonly observedSequence: bigint};

function selectTailRecords(
  records: readonly StoreRecord[],
  initial: bigint,
  sequencePolicy: 'contiguous' | 'lower-bound',
): TailSelection {
  let expected = initial;
  const selected: StoreRecord[] = [];
  for (const record of records) {
    if (record.seq < expected) continue;
    if (sequencePolicy === 'contiguous' && record.seq > expected) {
      return {kind: 'gap', expectedSequence: expected, observedSequence: record.seq};
    }
    selected.push(record);
    expected = record.seq + 1n;
  }
  return {kind: 'selected', records: selected, nextSequence: expected};
}

function refresh(
  state: RunDetailState,
  expected: bigint,
  observed: bigint,
  reason: Extract<RunDetailCursor, {kind: 'refresh-required'}>['reason'],
): RunDetailUpdate {
  const next = {
    ...state,
    cursor: {kind: 'refresh-required', nextSequence: expected, observedSequence: observed, reason},
  } satisfies RunDetailState;
  return {kind: 'refresh-required', state: next, reason};
}

function foldKeyframe(
  state: RunDetailState,
  keyframe: Keyframe,
  availableDescriptors: ReadonlyMap<string, DescriptorKnowledge>,
): RunDetailState {
  const lookup = foldKeyframeEpochs(state, keyframe, availableDescriptors);
  let next = state;
  for (const event of keyframeEvents(keyframe)) {
    switch (event.kind) {
      case 'epoch':
        next = foldEpoch(
          next,
          event.sequence,
          event.epoch,
          event.descriptorsSha256,
          availableDescriptors,
        );
        break;
      case 'lifecycle': {
        const epoch = requiredEpochAt(lookup, event.sequence, 'keyframe.lifecycles');
        next = foldLifecycle(next, event.sequence, epoch.epoch, event.lifecycle);
        break;
      }
      case 'span': {
        const epoch = requiredEpochAt(lookup, event.sequence, 'keyframe.spans');
        next = foldSpan(next, {
          sequence: event.sequence,
          epoch: epoch.epoch,
          span: event.span,
          startsSpan: event.startsSpan,
          startProof: spanStartProofOf(next, event.span.id),
        });
        break;
      }
      case 'metric': {
        const epoch = requiredEpochAt(lookup, event.sequence, 'keyframe.metrics');
        next = foldMetric(next, event.sequence, epoch.epoch, event.point);
        break;
      }
      default:
        event satisfies never;
    }
  }
  return {...next, throughSequence: keyframe.throughSeq};
}

type KeyframeEvent =
  | {
      readonly kind: 'epoch';
      readonly sequence: bigint;
      readonly epoch: EpochStarted;
      readonly descriptorsSha256: string | undefined;
      readonly startsEpoch: boolean;
    }
  | {
      readonly kind: 'lifecycle';
      readonly sequence: bigint;
      readonly lifecycle: import('@vibesys/proto').Lifecycle;
    }
  | {
      readonly kind: 'span';
      readonly sequence: bigint;
      readonly span: Span;
      readonly startsSpan: boolean;
    }
  | {
      readonly kind: 'metric';
      readonly sequence: bigint;
      readonly point: import('@vibesys/proto').MetricPoint;
    };

function keyframeEvents(keyframe: Keyframe): readonly KeyframeEvent[] {
  const events: KeyframeEvent[] = [];
  for (const version of keyframe.epochs) {
    if (!version.epoch) throw new Error('keyframe.epochs.epoch: required');
    events.push({
      kind: 'epoch',
      sequence: version.seq,
      epoch: version.epoch,
      descriptorsSha256: version.descriptorsSha256 || undefined,
      startsEpoch: version.startsEpoch,
    });
  }
  for (const version of keyframe.lifecycles) {
    if (!version.lifecycle) throw new Error('keyframe.lifecycles.lifecycle: required');
    events.push({kind: 'lifecycle', sequence: version.seq, lifecycle: version.lifecycle});
  }
  for (const history of Object.values(keyframe.spans)) {
    history.versions.forEach((version, index) => {
      if (!version.span) throw new Error('keyframe.spans.span: required');
      events.push({
        kind: 'span',
        sequence: version.seq,
        span: version.span,
        startsSpan: index === 0,
      });
    });
  }
  for (const history of keyframe.metrics) {
    for (const version of history.points) {
      if (!version.point) throw new Error('keyframe.metrics.point: required');
      events.push({kind: 'metric', sequence: version.seq, point: version.point});
    }
  }
  return events.sort(compareKeyframeEvents);
}

function compareKeyframeEvents(left: KeyframeEvent, right: KeyframeEvent): number {
  const sequence = compareBigint(left.sequence, right.sequence);
  if (sequence !== 0) return sequence;
  return keyframeEventOrder(left) - keyframeEventOrder(right);
}

function keyframeEventOrder(event: KeyframeEvent): number {
  if (event.kind === 'epoch') return event.startsEpoch ? 0 : 2;
  return 1;
}

function foldKeyframeEpochs(
  state: RunDetailState,
  keyframe: Keyframe,
  availableDescriptors: ReadonlyMap<string, DescriptorKnowledge>,
): RunDetailState {
  let next = state;
  for (const version of keyframe.epochs) {
    if (!version.epoch) throw new Error('keyframe.epochs.epoch: required');
    next = foldEpoch(
      next,
      version.seq,
      version.epoch,
      version.descriptorsSha256 || undefined,
      availableDescriptors,
    );
  }
  return next;
}

function foldRecord(state: RunDetailState, record: StoreRecord): RunDetailState {
  const {body} = record;
  switch (body.case) {
    case 'epochStarted':
    case 'declarationAmended':
      return foldEpoch(state, record.seq, body.value);
    case 'lifecycle':
      return foldLifecycle(state, record.seq, record.epoch, body.value);
    case 'span': {
      const span = body.value.span;
      if (!span) throw new Error(`record ${record.seq}: span.span is required`);
      return foldSpan(state, {
        sequence: record.seq,
        epoch: record.epoch,
        span,
        startsSpan: body.value.change === Change.STARTED,
        startProof: spanStartProofOf(state, span.id),
      });
    }
    case 'metric':
      return foldMetric(state, record.seq, record.epoch, body.value);
    case 'legacyEvent':
    case 'stream':
    case 'blob':
    case 'intent':
    case 'notice':
    case 'chat':
    case 'coreInput':
    case 'stepStarted':
    case 'stepFinished':
    case 'attemptInterrupted':
    case 'runEnded':
    case 'runStarted':
    case 'strategyAdvanced':
    case 'coreSnapshot':
    case undefined:
      return state;
    default:
      body satisfies never;
      return state;
  }
}

function foldEpoch(
  state: RunDetailState,
  sequence: bigint,
  epoch: EpochStarted,
  addressedDescriptors?: string,
  availableDescriptors: ReadonlyMap<string, DescriptorKnowledge> = new Map(
    state.descriptors.map(descriptor => [descriptor.sha256, descriptor]),
  ),
): RunDetailState {
  if (epoch.runId && epoch.runId !== state.runId) throw new Error('epoch.run_id: wrong run');
  const messageTypes = epoch.descriptors
    ? [...descriptorRegistry(epoch.descriptors)]
        .filter(descriptor => descriptor.kind === 'message')
        .map(descriptor => descriptor.typeName)
    : (availableDescriptors.get(addressedDescriptors ?? '')?.messageTypes ?? []);
  const knowledge: EpochKnowledge = {
    sequence,
    epoch: epoch.epoch,
    spanKinds: epoch.spanKinds.map(declaration => ({
      kind: declaration.kind,
      attrsType: declaration.attrsType,
      descriptorKnown: messageTypes.includes(declaration.attrsType),
    })),
    metrics: epoch.series.map(declaration => ({
      name: declaration.name,
      unit: declaration.unit,
      direction: DIRECTIONS[declaration.direction],
      role: ROLES[declaration.role],
    })),
    objectives:
      epoch.run?.objectives.flatMap(objective => {
        const direction = DIRECTIONS[objective.direction];
        return direction === 'max' || direction === 'min'
          ? [{series: objective.series, direction}]
          : [];
      }) ?? [],
  };
  const prior = state.epochs.filter(item => item.epoch === knowledge.epoch).at(-1);
  if (prior && declarationFingerprint(prior) === declarationFingerprint(knowledge)) return state;
  const epochs = [...state.epochs, knowledge].sort((left, right) =>
    compareBigint(left.sequence, right.sequence),
  );
  const excess = Math.max(0, epochs.length - state.limits.maxEpochVersions);
  return {
    ...state,
    epochs: excess === 0 ? epochs : epochs.slice(excess),
    metrics: rebindMetricDeclarations(state.metrics, knowledge),
    truncation: {...state.truncation, epochEvictions: state.truncation.epochEvictions + excess},
  };
}

function declarationFingerprint(knowledge: EpochKnowledge): string {
  return JSON.stringify([knowledge.spanKinds, knowledge.metrics, knowledge.objectives]);
}

function rebindMetricDeclarations(
  metrics: readonly MetricSeriesState[],
  knowledge: EpochKnowledge,
): readonly MetricSeriesState[] {
  const source = {
    kind: 'run-record' as const,
    sequence: knowledge.sequence,
    epoch: knowledge.epoch,
  };
  return metrics.map(series => {
    if (series.epoch !== knowledge.epoch) return series;
    const declaration = knowledge.metrics.find(item => item.name === series.name);
    if (!declaration?.direction || !declaration.role) return series;
    return {
      ...series,
      unit: declaration.unit,
      direction: declaration.direction,
      role: declaration.role,
      source,
    };
  });
}

function foldLifecycle(
  state: RunDetailState,
  sequence: bigint,
  epoch: number,
  lifecycle: Parameters<typeof lifecycleValue>[0],
): RunDetailState {
  const value = lifecycleValue(lifecycle);
  if (!value) throw new Error('lifecycle.state: unsupported');
  const projected: LifecycleProjection = {
    value,
    source: {kind: 'run-record', sequence, epoch},
    scope: {kind: 'run', runId: state.runId},
  };
  return {...state, lifecycle: projected};
}

function lifecycleValue(lifecycle: import('@vibesys/proto').Lifecycle) {
  const {state} = lifecycle;
  switch (state.case) {
    case 'starting':
      return 'starting' as const;
    case 'running':
      return state.value.hold ? ('held' as const) : ('running' as const);
    case 'draining':
      return 'draining' as const;
    case 'parked':
      return 'parked' as const;
    case 'ended':
      switch (state.value.outcome) {
        case Outcome.FINISHED:
          return 'finished' as const;
        case Outcome.FAILED:
          return 'failed' as const;
        case Outcome.STOPPED:
          return 'stopped' as const;
        case Outcome.INTERRUPTED:
          return 'interrupted' as const;
        default:
          return undefined;
      }
    case undefined:
      return undefined;
    default:
      state satisfies never;
      return undefined;
  }
}

type SpanDeclarationProof = RunDetailState['agents'][number]['startProof'];

interface SpanFoldInput {
  readonly sequence: bigint;
  readonly epoch: number;
  readonly span: Span;
  readonly startsSpan: boolean;
  readonly startProof: SpanDeclarationProof | undefined;
}

function foldSpan(state: RunDetailState, input: SpanFoldInput): RunDetailState {
  const {sequence, epoch, span} = input;
  const next = {
    ...state,
    agents: state.agents.filter(agent => agent.spanId !== span.id),
    workstreams: state.workstreams.filter(workstream => workstream.spanId !== span.id),
    correctnessGates: state.correctnessGates.filter(gate => gate.spanId !== span.id),
    issues: state.issues.filter(issue => issue.spanId !== span.id),
  };
  const label = span.label;
  if (!label) return next;
  const attrsType = typeName(label.attrs?.typeUrl ?? '');
  const issue = {
    spanId: span.id,
    kind: label.kind,
    attrsType,
    sequence,
    epoch,
  };
  const resolution = resolveSpanStart(state, input, label.kind, attrsType);
  if (resolution.kind === 'issue') {
    return addIssue(next, {
      ...issue,
      declaredAttrsType: resolution.declaredAttrsType,
      startedSequence: input.startsSpan ? sequence : input.startProof?.startedSequence,
      startedEpoch: input.startsSpan ? epoch : input.startProof?.startedEpoch,
      startProof: undefined,
      reason: resolution.reason,
    });
  }
  const {proof: startProof} = resolution;
  if (startProof.kind !== label.kind || startProof.attrsType !== attrsType) {
    if (state.truncation.epochEvictions > 0) throw new HistoryEvictedError();
    return addIssue(next, {
      ...issue,
      declaredAttrsType: startProof.attrsType,
      startedSequence: startProof.startedSequence,
      startedEpoch: startProof.startedEpoch,
      startProof,
      reason: 'descriptor-mismatch',
    });
  }
  const projected = projectTypedSpan({
    runId: state.runId,
    sequence,
    epoch,
    startProof,
    attrsType,
    span,
  });
  switch (projected.kind) {
    case 'agent':
      return addAgent(next, projected.value);
    case 'workstream':
      return addWorkstream(next, projected.value);
    case 'correctness':
      return addCorrectnessGate(next, projected.value);
    case 'unsupported':
      return addIssue(next, {
        ...issue,
        declaredAttrsType: startProof.attrsType,
        startedSequence: startProof.startedSequence,
        startedEpoch: startProof.startedEpoch,
        startProof,
        reason: 'projector-not-installed',
      });
    case 'invalid':
      return addIssue(next, {
        ...issue,
        declaredAttrsType: startProof.attrsType,
        startedSequence: startProof.startedSequence,
        startedEpoch: startProof.startedEpoch,
        startProof,
        reason: 'invalid-attributes',
      });
  }
}

function resolveSpanStart(
  state: RunDetailState,
  input: SpanFoldInput,
  kind: string,
  attrsType: string,
):
  | {readonly kind: 'resolved'; readonly proof: SpanDeclarationProof}
  | {
      readonly kind: 'issue';
      readonly reason: AttributeIssue['reason'];
      readonly declaredAttrsType: string | undefined;
    } {
  if (input.startProof) return {kind: 'resolved', proof: input.startProof};
  if (input.startsSpan) return spanStartProof(state, input, kind, attrsType);
  if (
    state.truncation.agentEvictions > 0 ||
    state.truncation.workstreamEvictions > 0 ||
    state.truncation.correctnessGateEvictions > 0 ||
    state.truncation.issueEvictions > 0
  ) {
    throw new HistoryEvictedError();
  }
  return {kind: 'issue', reason: 'missing-span-start', declaredAttrsType: undefined};
}

function spanStartProof(
  state: RunDetailState,
  input: SpanFoldInput,
  kind: string,
  attrsType: string,
):
  | {readonly kind: 'resolved'; readonly proof: SpanDeclarationProof}
  | {
      readonly kind: 'issue';
      readonly reason: 'undeclared-kind' | 'descriptor-mismatch' | 'descriptor-unknown';
      readonly declaredAttrsType: string | undefined;
    } {
  const epochKnowledge = epochVersionAt(state, input.epoch, input.sequence);
  if (!epochKnowledge && state.truncation.epochEvictions > 0) throw new HistoryEvictedError();
  const declaration = epochKnowledge?.spanKinds.find(item => item.kind === kind);
  if (!declaration) return {kind: 'issue', reason: 'undeclared-kind', declaredAttrsType: undefined};
  if (declaration.attrsType !== attrsType) {
    return {
      kind: 'issue',
      reason: 'descriptor-mismatch',
      declaredAttrsType: declaration.attrsType,
    };
  }
  if (!declaration.descriptorKnown) {
    return {
      kind: 'issue',
      reason: 'descriptor-unknown',
      declaredAttrsType: declaration.attrsType,
    };
  }
  return {
    kind: 'resolved',
    proof: {
      startedSequence: input.sequence,
      startedEpoch: input.epoch,
      kind,
      attrsType,
    },
  };
}

function addAgent(state: RunDetailState, projected: AgentState): RunDetailState {
  const agents = [...state.agents, projected].sort((left, right) =>
    compareSource(left.source.sequence, left.spanId, right.source.sequence, right.spanId),
  );
  const excess = Math.max(0, agents.length - state.limits.maxAgents);
  return {
    ...state,
    agents: excess === 0 ? agents : agents.slice(excess),
    truncation: {...state.truncation, agentEvictions: state.truncation.agentEvictions + excess},
  };
}

function addWorkstream(state: RunDetailState, projected: WorkstreamState): RunDetailState {
  const workstreams = [...state.workstreams, projected].sort((left, right) =>
    compareSource(left.source.sequence, left.spanId, right.source.sequence, right.spanId),
  );
  const excess = Math.max(0, workstreams.length - state.limits.maxWorkstreams);
  return {
    ...state,
    workstreams: excess === 0 ? workstreams : workstreams.slice(excess),
    truncation: {
      ...state.truncation,
      workstreamEvictions: state.truncation.workstreamEvictions + excess,
    },
  };
}

function addCorrectnessGate(
  state: RunDetailState,
  projected: CorrectnessGateState,
): RunDetailState {
  const correctnessGates = [...state.correctnessGates, projected].sort((left, right) =>
    compareSource(left.source.sequence, left.spanId, right.source.sequence, right.spanId),
  );
  const excess = Math.max(0, correctnessGates.length - state.limits.maxCorrectnessGates);
  return {
    ...state,
    correctnessGates: excess === 0 ? correctnessGates : correctnessGates.slice(excess),
    truncation: {
      ...state.truncation,
      correctnessGateEvictions: state.truncation.correctnessGateEvictions + excess,
    },
  };
}

function spanStartProofOf(state: RunDetailState, spanId: string): SpanDeclarationProof | undefined {
  const projected = [...state.agents, ...state.workstreams, ...state.correctnessGates].find(
    item => item.spanId === spanId,
  );
  return projected?.startProof ?? state.issues.find(item => item.spanId === spanId)?.startProof;
}

function foldMetric(
  state: RunDetailState,
  sequence: bigint,
  epoch: number,
  point: import('@vibesys/proto').MetricPoint,
  declarations: RunDetailState = state,
): RunDetailState {
  const existing = state.metrics.find(item => item.epoch === epoch && item.name === point.series);
  if (!existing && state.truncation.metricSeriesEvictions > 0) {
    throw new HistoryEvictedError();
  }
  const epochVersion = epochVersionAt(declarations, epoch, sequence);
  const declared = epochVersion?.metrics.find(item => item.name === point.series);
  const declaration =
    epochVersion === undefined && existing
      ? {
          name: existing.name,
          unit: existing.unit,
          direction: existing.direction,
          role: existing.role,
        }
      : declared;
  if (epochVersion === undefined && !declaration && state.truncation.epochEvictions > 0) {
    throw new HistoryEvictedError();
  }
  if (!declaration?.direction || !declaration.role) {
    return {...state, undeclaredMetricPoints: state.undeclaredMetricPoints + 1};
  }
  const source = {kind: 'run-record' as const, sequence, epoch};
  const scope = point.span
    ? ({kind: 'span', runId: state.runId, spanId: point.span} as const)
    : ({kind: 'run', runId: state.runId} as const);
  const points = [
    ...(existing?.points ?? []),
    {value: point.value, clock: point.clock, source, scope},
  ];
  const pointExcess = Math.max(0, points.length - state.limits.maxMetricPointsPerSeries);
  const series: MetricSeriesState = {
    name: declaration.name,
    unit: declaration.unit,
    direction: declaration.direction,
    role: declaration.role,
    source:
      epochVersion === undefined
        ? (existing?.source ?? {kind: 'run-record', sequence, epoch})
        : {kind: 'run-record', sequence: epochVersion.sequence, epoch},
    scope: {kind: 'run', runId: state.runId},
    points: pointExcess === 0 ? points : points.slice(pointExcess),
    omittedPoints: (existing?.omittedPoints ?? 0) + pointExcess,
    epoch,
  };
  const metrics = [
    ...state.metrics.filter(item => item.epoch !== epoch || item.name !== point.series),
    series,
  ].sort((left, right) => {
    const leftSequence = left.points.at(-1)?.source.sequence ?? left.source.sequence;
    const rightSequence = right.points.at(-1)?.source.sequence ?? right.source.sequence;
    return compareSource(leftSequence, left.name, rightSequence, right.name);
  });
  const excess = Math.max(0, metrics.length - state.limits.maxMetricSeries);
  return {
    ...state,
    metrics: excess === 0 ? metrics : metrics.slice(excess),
    truncation: {
      ...state.truncation,
      metricSeriesEvictions: state.truncation.metricSeriesEvictions + excess,
    },
  };
}

function addDescriptor(state: RunDetailState, descriptor: DescriptorKnowledge): RunDetailState {
  const descriptors = [
    ...state.descriptors.filter(item => item.sha256 !== descriptor.sha256),
    descriptor,
  ];
  const excess = Math.max(0, descriptors.length - state.limits.maxDescriptorSets);
  return {
    ...state,
    descriptors: excess === 0 ? descriptors : descriptors.slice(excess),
    truncation: {
      ...state.truncation,
      descriptorSetEvictions: state.truncation.descriptorSetEvictions + excess,
    },
  };
}

function addIssue(state: RunDetailState, issue: AttributeIssue): RunDetailState {
  const issues = [...state.issues.filter(item => item.spanId !== issue.spanId), issue].sort(
    (left, right) => compareSource(left.sequence, left.spanId, right.sequence, right.spanId),
  );
  const excess = Math.max(0, issues.length - state.limits.maxIssues);
  return {
    ...state,
    issues: excess === 0 ? issues : issues.slice(excess),
    truncation: {...state.truncation, issueEvictions: state.truncation.issueEvictions + excess},
  };
}

function finalizeEvidence(state: RunDetailState, limits: RunDetailLimits): RunDetailState {
  const agentExcess = Math.max(0, state.agents.length - limits.maxAgents);
  const workstreamExcess = Math.max(0, state.workstreams.length - limits.maxWorkstreams);
  const correctnessGateExcess = Math.max(
    0,
    state.correctnessGates.length - limits.maxCorrectnessGates,
  );
  const epochExcess = Math.max(0, state.epochs.length - limits.maxEpochVersions);
  const issueExcess = Math.max(0, state.issues.length - limits.maxIssues);
  const boundedMetrics = state.metrics.map(series => {
    const excess = Math.max(0, series.points.length - limits.maxMetricPointsPerSeries);
    return {
      ...series,
      points: excess === 0 ? series.points : series.points.slice(excess),
      omittedPoints: series.omittedPoints + excess,
    };
  });
  const metricSeriesExcess = Math.max(0, boundedMetrics.length - limits.maxMetricSeries);
  return {
    ...state,
    limits,
    agents: agentExcess === 0 ? state.agents : state.agents.slice(agentExcess),
    workstreams:
      workstreamExcess === 0 ? state.workstreams : state.workstreams.slice(workstreamExcess),
    correctnessGates:
      correctnessGateExcess === 0
        ? state.correctnessGates
        : state.correctnessGates.slice(correctnessGateExcess),
    epochs: epochExcess === 0 ? state.epochs : state.epochs.slice(epochExcess),
    issues: issueExcess === 0 ? state.issues : state.issues.slice(issueExcess),
    metrics: metricSeriesExcess === 0 ? boundedMetrics : boundedMetrics.slice(metricSeriesExcess),
    truncation: {
      agentEvictions: state.truncation.agentEvictions + agentExcess,
      correctnessGateEvictions: state.truncation.correctnessGateEvictions + correctnessGateExcess,
      descriptorSetEvictions: state.truncation.descriptorSetEvictions,
      epochEvictions: state.truncation.epochEvictions + epochExcess,
      issueEvictions: state.truncation.issueEvictions + issueExcess,
      metricSeriesEvictions: state.truncation.metricSeriesEvictions + metricSeriesExcess,
      workstreamEvictions: state.truncation.workstreamEvictions + workstreamExcess,
    },
  };
}

function epochVersionAt(
  state: RunDetailState,
  epoch: number,
  sequence: bigint,
): EpochKnowledge | undefined {
  return state.epochs.filter(item => item.epoch === epoch && item.sequence <= sequence).at(-1);
}

function epochAt(state: RunDetailState, sequence: bigint): EpochKnowledge | undefined {
  return state.epochs.filter(item => item.sequence <= sequence).at(-1);
}

function requiredEpochAt(state: RunDetailState, sequence: bigint, path: string): EpochKnowledge {
  const epoch = epochAt(state, sequence);
  if (!epoch) throw new Error(`${path}: no epoch at sequence ${sequence}`);
  return epoch;
}

function validateSnapshotRecords(
  initialSequence: bigint | undefined,
  records: readonly StoreRecord[],
  sequencePolicy: 'contiguous' | 'lower-bound',
): void {
  let previous = initialSequence;
  for (const record of records) {
    const expected = previous === undefined ? 1n : previous + 1n;
    if (
      (sequencePolicy === 'lower-bound' && previous !== undefined && record.seq <= previous) ||
      (sequencePolicy === 'contiguous' && record.seq !== expected)
    ) {
      throw new Error(`skeleton.records.seq: expected ${expected}, got ${record.seq}`);
    }
    previous = record.seq;
  }
}

function validateKeyframe(keyframe: Keyframe): void {
  if (keyframe.throughSeq <= 0n) throw new Error('keyframe.through_seq: must be positive');
  validateSpanHistories(keyframe);
  validateMetricHistories(keyframe);
  validateKeyframeIndexes(keyframe);
}

function validateSpanHistories(keyframe: Keyframe): void {
  for (const [id, history] of Object.entries(keyframe.spans)) {
    ordered(history.versions, keyframe.throughSeq, `keyframe.spans[${id}]`);
    for (const version of history.versions) {
      if (!version.span || version.span.id !== id)
        throw new Error(`keyframe.spans[${id}]: mismatched span`);
    }
  }
}

function validateMetricHistories(keyframe: Keyframe): void {
  const series = new Set<string>();
  for (const history of keyframe.metrics) {
    if (series.has(history.series))
      throw new Error(`keyframe.metrics: duplicate ${history.series}`);
    series.add(history.series);
    ordered(history.points, keyframe.throughSeq, `keyframe.metrics[${history.series}]`);
    for (const version of history.points) {
      if (!version.point || version.point.series !== history.series)
        throw new Error(`keyframe.metrics[${history.series}]: mismatched point`);
    }
  }
}

function validateKeyframeIndexes(keyframe: Keyframe): void {
  ordered(keyframe.epochs, keyframe.throughSeq, 'keyframe.epochs');
  ordered(keyframe.lifecycles, keyframe.throughSeq, 'keyframe.lifecycles');
  if (keyframe.epochs.some(version => !version.epoch))
    throw new Error('keyframe.epochs.epoch: required');
  if (keyframe.lifecycles.some(version => !version.lifecycle))
    throw new Error('keyframe.lifecycles.lifecycle: required');
  const streams = new Set<string>();
  for (const stream of keyframe.streams) {
    if (streams.has(stream.id)) throw new Error(`keyframe.streams: duplicate ${stream.id}`);
    streams.add(stream.id);
  }
}

function ordered(values: readonly {readonly seq: bigint}[], through: bigint, path: string): void {
  let previous = 0n;
  for (const value of values) {
    if (value.seq <= previous || value.seq > through) throw new Error(`${path}.seq: invalid order`);
    previous = value.seq;
  }
}

function typeName(typeUrl: string): string {
  return typeUrl.split('/').at(-1) ?? '';
}

function compareBigint(left: bigint, right: bigint): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

function compareSource(left: bigint, leftId: string, right: bigint, rightId: string): number {
  const sequence = compareBigint(left, right);
  return sequence === 0 ? leftId.localeCompare(rightId) : sequence;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
